"""ASGI integration (FastAPI, Starlette and other ASGI applications).

Usage (FastAPI / Starlette)::

    from fastapi import FastAPI
    from vitalwatch.asgi import VitalWatchASGIMiddleware

    app = FastAPI()
    app.add_middleware(VitalWatchASGIMiddleware)

Any ASGI application can be wrapped directly::

    app = VitalWatchASGIMiddleware(app)

This is plain ASGI middleware: Starlette and FastAPI are **not** imported
when this module is imported; they are only looked up lazily during a request,
if present. ``import vitalwatch`` keeps working in framework-less projects.

How an exception travels in Starlette (verified with 1.6):

- ``ServerErrorMiddleware`` sits **outside** user middleware; an uncaught
  error passes through our layer to reach it, and it writes a 500 and
  re-raises. An ``@app.exception_handler(Exception)`` (or 500) handler is also
  attached to ``ServerErrorMiddleware`` — the error still passes through us.
- Handlers bound to a specific class (``@app.exception_handler(ValueError)``)
  and ``HTTPException`` are handled in the inner ``ExceptionMiddleware``; the
  error never reaches us. So that the stack is not lost when such a handler
  returns a 5xx, these handlers are wrapped per instance (the counterpart of
  the ``handle_user_exception`` wrapper in Flask). ``ExceptionMiddleware`` is
  looked up along the ``.app`` chain on the first request, so middleware order
  does not matter. If the app is wrapped by hand
  (``VitalWatchASGIMiddleware(app)``), Starlette builds its stack on the first
  request, so the wrapping takes effect **after** the first request.
"""

import time

from . import hook_process, reporter, scrubber

#: Bu istekte hata stack'iyle raporlandı; "HTTP 500" ayrıca açılmaz.
#: Flask'taki ``g._vitalwatch_reported`` işaretinin karşılığı; scope üzerinde taşınır.
_REPORTED = "vitalwatch.reported"

#: İsteğin bu katmana girdiği andaki ``root_path``; Mount öneki bundan hesaplanır.
_ROOT_PATH = "vitalwatch.root_path"

#: Sarılmış işleyiciyi ikinci kez sarmamak için işaret.
_WRAPPED = "_vitalwatch_wrapped"

#: Bu istek dıştaki bir VitalWatch katmanında ölçülüyor; içteki katman (ör. Mount
#: edilmiş alt uygulamanınki) yalnızca geçirir — tek istek, tek kayıt.
_ACTIVE = "vitalwatch.active"

#: ``.app`` zincirinde ExceptionMiddleware aranırken inilecek en fazla kat.
_MAX_DEPTH = 32

#: ExceptionMiddleware bulunamazsa kaç istek daha denenir (elle sarmalanan
#: Starlette uygulaması yığınını ilk istekte kurar).
_MAX_ATTEMPTS = 3


class VitalWatchASGIMiddleware:
    """Reports slow requests, 5xx responses and uncaught exceptions.

    :param app: The inner ASGI application.
    :param hook_process_errors: Also install the uncaught-exception hooks and
        the heartbeat timer. On by default (same as the Flask extension).
    """

    def __init__(self, app, hook_process_errors=True):
        self.app = app
        self._handlers_found = False
        self._attempts = 0

        # Raporlayıcı burada yakalanmaz; her çağrıda reporter() ile çözülür.
        self._wrap_handlers()

        if hook_process_errors:
            # Süreç başına bir kez kurulur (hook_process kendisi korur);
            # Starlette her yığın kurulumunda bu sınıfı yeniden örnekliyor.
            try:
                hook_process()
            except Exception:  # noqa: BLE001
                pass

    def _wrap_handlers(self):
        if self._handlers_found or self._attempts > _MAX_ATTEMPTS:
            return

        self._attempts += 1

        try:
            middleware = _find_exception_middleware(self.app)

            if middleware is not None:
                _wrap_exception_handlers(middleware)
                self._handlers_found = True
        except Exception:  # noqa: BLE001
            pass

    async def __call__(self, scope, receive, send):
        self._wrap_handlers()

        # Yalnızca HTTP ölçülür; lifespan ve websocket olduğu gibi geçer.
        # Dıştaki bir VitalWatch katmanı zaten ölçüyorsa bu katman yalnızca geçirir.
        if scope.get("type") != "http" or scope.get(_ACTIVE):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        scope[_ACTIVE] = True
        scope[_ROOT_PATH] = scope.get("root_path", "") or ""
        state = {"code": None, "ended": None}

        async def send_wrapper(message):
            try:
                kind = message.get("type")

                if kind == "http.response.start":
                    state["code"] = int(message.get("status"))
            except Exception:  # noqa: BLE001
                kind = None

            await send(message)

            # Süre son gövde parçası gidince durur: BackgroundTasks ve yanıt
            # sonrası işler "yavaş istek" sayılmasın.
            try:
                if kind == "http.response.body" and not message.get("more_body", False):
                    if state["ended"] is None:
                        state["ended"] = time.perf_counter()
            except Exception:  # noqa: BLE001
                pass

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as error:
            # Hata **yutulmaz**: raporlanır ve olduğu gibi yeniden fırlatılır;
            # ServerErrorMiddleware / sunucu kendi 500'ünü yazar.
            if _should_report(error):
                _report(error, scope)
            raise
        finally:
            _measure(scope, state["code"], started, state["ended"])


def _should_report(error):
    """Yeniden fırlatılacak hatanın raporlanıp raporlanmayacağı; asla fırlatmaz."""
    try:
        return isinstance(error, Exception) and not _client_error(error)
    except Exception:  # noqa: BLE001
        return False


def _find_exception_middleware(app):
    """``.app`` (ya da Starlette'in ``middleware_stack``) zincirinde, sınıfa
    bağlı işleyicileri tutan ilk katman."""
    node = app

    for _ in range(_MAX_DEPTH):
        if node is None:
            return None

        if isinstance(getattr(node, "_exception_handlers", None), dict):
            return node

        inner = getattr(node, "app", None)

        if inner is None:
            inner = getattr(node, "middleware_stack", None)

        if inner is node:
            return None

        node = inner

    return None


def _report(error, scope):
    try:
        current = reporter()

        if not current.configured():
            return

        current.record_exception(
            error,
            route=route_pattern(scope),
            method=scope.get("method"),
        )
        scope[_REPORTED] = True
    except Exception:  # noqa: BLE001
        pass


def _measure(scope, status, started, ended=None):
    try:
        # Yanıt başlamadıysa ölçülecek durum kodu yok.
        if status is None:
            return

        # Hata stack'iyle raporlandı; "HTTP 500" aynı arızanın tekrarı.
        if status >= 500 and scope.get(_REPORTED):
            return

        duration_ms = ((ended if ended is not None else time.perf_counter()) - started) * 1000
        current = reporter()

        # Rota deseni (yönlendirici taraması) yalnızca olay gidecekse hesaplanır.
        if not current.wants_request(status, duration_ms):
            return

        current.record_request(
            route=route_pattern(scope),
            method=scope.get("method"),
            status=status,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001
        # İzleme kodu isteği bozmaz.
        pass


def route_pattern(scope):
    """The matched route pattern (``/product/{product_id}``), not the actual path.

    Order: FastAPI's ``scope["route"]``, then the endpoint's pattern in the
    Starlette router tree, and as a last resort the actual path without the
    query string (M3). For routes under ``Mount``, the prefix between the root
    path when the request entered this layer and the later root is prepended.
    """
    root_path = scope.get(_ROOT_PATH, "") or ""

    try:
        pattern = _fastapi_pattern(scope, root_path) or _starlette_pattern(scope, root_path)
    except Exception:  # noqa: BLE001
        pattern = None

    if pattern:
        return scrubber.text(pattern, 300) or "/"

    return scrubber.path(scope.get("path")) or "/"


def _fastapi_pattern(scope, root_path):
    route = scope.get("route")
    path = getattr(route, "path", None)

    if not isinstance(path, str) or not path:
        return None

    return _mount_prefix(scope, root_path) + path


def _mount_prefix(scope, root_path):
    """Mount'un eklediği önek: çağrı sonrası root_path ile giriş anındaki fark."""
    current = scope.get("root_path", "") or ""

    if root_path and current.startswith(root_path):
        return current[len(root_path):]

    return current if not root_path else ""


def _starlette_pattern(scope, root_path):
    """Starlette ``scope["route"]`` koymuyor; uç nokta yönlendiricide aranır.

    Aynı uç nokta birden çok yola bağlı olabilir; bu yüzden aday desen
    derlenip isteğin yoluyla eşleştirilir.
    """
    router = scope.get("router")
    endpoint = scope.get("endpoint")

    if router is None or endpoint is None:
        return None

    try:
        from starlette.routing import compile_path
    except ImportError:  # pragma: no cover
        return None

    path = scope.get("path") or ""

    if root_path and path.startswith(root_path):
        path = path[len(root_path):] or "/"

    for pattern, regex in _compiled_candidates(router, endpoint, compile_path):
        if regex.match(path):
            return pattern

    return None


#: (id(router), id(endpoint), rota sayısı) → (router, endpoint, [(desen, regex)]).
#: Nesnelerin kendisi de tutulur: id yeniden kullanılırsa yanlış eşleşmesin.
_CACHE = {}
_CACHE_LIMIT = 1024


def _compiled_candidates(router, endpoint, compile_path):
    routes = getattr(router, "routes", ()) or ()

    try:
        key = (id(router), id(endpoint), len(routes))
    except Exception:  # noqa: BLE001
        key = None

    cached = _CACHE.get(key) if key is not None else None

    if cached is not None and cached[0] is router and cached[1] is endpoint:
        return cached[2]

    compiled = []

    for pattern in _candidates(routes, endpoint, "", 0):
        try:
            compiled.append((pattern, compile_path(pattern)[0]))
        except Exception:  # noqa: BLE001
            continue

    if key is not None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()

        _CACHE[key] = (router, endpoint, compiled)

    return compiled


def _candidates(routes, endpoint, prefix, depth):
    if depth > 8:
        return

    for route in routes or ():
        path = getattr(route, "path", None)

        if not isinstance(path, str):
            continue

        if getattr(route, "endpoint", None) is endpoint:
            yield prefix + path

        children = getattr(route, "routes", None)

        if children:
            yield from _candidates(children, endpoint, prefix + path, depth + 1)


def _client_error(error):
    """``HTTPException`` 4xx: bilinçli yanıt, arıza değil.

    Starlette/FastAPI import edilmez; ``status_code`` taşıyan ve adı
    ``HTTPException`` olan her sınıf aynı muamele görür.
    """
    status = getattr(error, "status_code", None)

    if not isinstance(status, int) or status >= 500:
        return False

    return any(cls.__name__ == "HTTPException" for cls in type(error).__mro__)


def _wrap_exception_handlers(app):
    """Starlette ``ExceptionMiddleware`` işleyicilerini örnek düzeyinde sarar.

    ``add_middleware`` ile kurulunca içteki uygulama ``ExceptionMiddleware``
    olur. Sınıfa bağlı işleyici hatayı "işlenmiş" sayar ve hata bize ulaşmaz;
    işleyici 5xx dönerse stack yalnızca burada görülebilir. Dönüş aynen iletilir.
    """
    try:
        handlers = getattr(app, "_exception_handlers", None)

        if not isinstance(handlers, dict):
            return

        for key, handler in list(handlers.items()):
            if getattr(handler, _WRAPPED, False) or not callable(handler):
                continue

            # HTTPException bilinçli yanıttır (Flask'taki gibi hiç sarılmaz).
            if isinstance(key, type) and _http_exception_class(key):
                continue

            handlers[key] = _wrap_handler(handler)
    except Exception:  # noqa: BLE001
        pass


def _http_exception_class(cls):
    return any(base.__name__ in ("HTTPException", "WebSocketException") for base in cls.__mro__)


def _wrap_handler(handler):
    import asyncio
    import functools
    import inspect

    # HTTPException işleyicileri hiç sarılmaz (bkz. _wrap_exception_handlers);
    # buraya yalnızca uygulama hatası düşer.
    def after(conn, error, response):
        try:
            status = getattr(response, "status_code", None)

            if isinstance(status, int) and status >= 500:
                scope = getattr(conn, "scope", None)

                if isinstance(scope, dict):
                    _report(error, scope)
        except Exception:  # noqa: BLE001
            pass

    target = handler
    while isinstance(target, functools.partial):
        target = target.func

    if asyncio.iscoroutinefunction(target) or inspect.iscoroutinefunction(
        getattr(target, "__call__", None)
    ):

        async def wrapped(conn, error):
            response = await handler(conn, error)
            after(conn, error, response)
            return response

    else:

        def wrapped(conn, error):
            response = handler(conn, error)
            after(conn, error, response)
            return response

    setattr(wrapped, _WRAPPED, True)

    return wrapped
