"""Django integration: slow requests, 5xx responses and uncaught exceptions.

Usage (``settings.py``)::

    MIDDLEWARE = [
        "vitalwatch.django.VitalWatchMiddleware",  # near the top of the list
        ...
    ]

Process hooks (``hook_process``) are installed once on first setup; set
``VITALWATCH_HOOK_PROCESS = False`` to turn them off.

Django is imported **inside** this module only; the package itself does not
depend on Django, and ``import vitalwatch`` works in projects without it.

Errors are seen through two paths, deduplicated by a marker on the exception:

- The ``got_request_exception`` signal: sent when Django **turns an exception
  into a 500** (not sent for ``Http404``, ``PermissionDenied``, ``BadRequest``
  or ``SuspiciousOperation``). Besides views it also covers errors raised by
  middleware below ours, which ``process_exception`` does not see.
- ``process_exception``: called for view errors only; the exception is noted
  on the request and reported at the end of the request if the response is a
  5xx and the signal was missed. Returns ``None`` — Django's own handling
  continues unchanged.
"""

import sys
import threading
import time

from . import hook_process, reporter, scrubber

#: İstek başlangıcını ``request`` üzerinde taşıyan alan adı.
_STARTED_AT = "_vitalwatch_started_at"

#: Bu istekte hata stack'iyle raporlandı; "HTTP 500" ayrıca açılmaz.
_REPORTED = "_vitalwatch_reported"

#: ``process_exception``'ın gördüğü, henüz raporlanmamış istisna.
_PENDING = "_vitalwatch_exception"

_setup_lock = threading.Lock()
_setup_done = False


class VitalWatchMiddleware:
    """New-style Django middleware (takes ``get_response``)."""

    def __init__(self, get_response):
        self.get_response = get_response

        # Raporlayıcı burada yakalanmaz; her çağrıda reporter() ile çözülür.
        _setup()

    def __call__(self, request):
        try:
            setattr(request, _STARTED_AT, time.perf_counter())
        except Exception:  # noqa: BLE001
            pass

        response = self.get_response(request)

        _measure(request, response)

        return response

    def process_exception(self, request, exception):
        # Hata **yutulmaz**: None dönülür, Django işleyişine devam eder.
        try:
            if not _client_error(exception):
                setattr(request, _PENDING, exception)
        except Exception:  # noqa: BLE001
            pass

        return None


def _setup():
    """Sinyal ve süreç kancaları süreç başına bir kez kurulur.

    ``hook_process`` her çağrıda excepthook'u bir kat daha zincirler; Django
    ara katmanı birden çok kez örnekleyebildiği için koruma burada.
    """
    global _setup_done

    with _setup_lock:
        if _setup_done:
            return

        _setup_done = True

    try:
        from django.core.signals import got_request_exception

        got_request_exception.connect(
            _on_request_exception, weak=False, dispatch_uid="vitalwatch.django"
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        from django.conf import settings

        enabled = getattr(settings, "VITALWATCH_HOOK_PROCESS", True)
    except Exception:  # noqa: BLE001
        enabled = True

    if enabled:
        try:
            hook_process()
        except Exception:  # noqa: BLE001
            pass


def _on_request_exception(sender=None, request=None, **kwargs):
    """Django istisnayı 500'e çeviriyor; istisna ``sys.exc_info`` içinde."""
    try:
        error = sys.exc_info()[1] or getattr(request, _PENDING, None)

        if error is None or _client_error(error):
            return

        _report(error, request)
    except Exception:  # noqa: BLE001
        pass


def _report(error, request):
    try:
        current = reporter()

        if not current.configured():
            return

        current.record_exception(
            error,
            route=route_pattern(request) if request is not None else None,
            method=getattr(request, "method", None),
        )

        if request is not None:
            setattr(request, _REPORTED, True)
    except Exception:  # noqa: BLE001
        pass


def _measure(request, response):
    try:
        status = int(getattr(response, "status_code", 0) or 0)

        # Sinyal kaçtıysa (ör. başka bir alıcı hatayı engelledi) not edilen
        # istisna burada, yanıt gerçekten 5xx ise raporlanır.
        pending = getattr(request, _PENDING, None)

        if status >= 500 and pending is not None and not getattr(request, _REPORTED, False):
            _report(pending, request)

        # Hata stack'iyle raporlandı; "HTTP 500" aynı arızanın tekrarı.
        if status >= 500 and getattr(request, _REPORTED, False):
            return

        started = getattr(request, _STARTED_AT, None)

        if started is None:
            return

        duration_ms = (time.perf_counter() - started) * 1000
        current = reporter()

        # Rota deseni yalnızca olay gerçekten gidecekse hesaplanır.
        if not current.wants_request(status, duration_ms):
            return

        current.record_request(
            route=route_pattern(request),
            method=request.method,
            status=status,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001
        # İzleme kodu isteği bozmaz.
        pass


def route_pattern(request):
    """Django's matched route pattern (``/product/<int:product_id>/``), not the actual path.

    For ``re_path`` patterns the leading ``^`` and trailing ``$`` are removed.
    Without a pattern (404, error before resolving) it falls back to the path
    with the query string removed (M3). The pattern is not passed through
    ``scrubber.path``: the ``?`` in ``(?P<id>...)`` would be taken for a query
    string and the pattern cut off.
    """
    try:
        match = getattr(request, "resolver_match", None)
        route = getattr(match, "route", None) if match is not None else None
    except Exception:  # noqa: BLE001
        route = None

    if isinstance(route, str):
        route = route.lstrip("^")

        if route.endswith("$"):
            route = route[:-1]

        return scrubber.text("/" + route.lstrip("/"), 300) or "/"

    return scrubber.path(getattr(request, "path", None)) or "/"


def _client_error(error):
    """Django'nun 4xx'e çevirdiği istisnalar: bilinçli yanıt, arıza değil."""
    try:
        from django.core.exceptions import BadRequest, PermissionDenied, SuspiciousOperation
        from django.http import Http404
        from django.http.multipartparser import MultiPartParserError
    except ImportError:  # pragma: no cover
        return False

    return isinstance(
        error, (Http404, PermissionDenied, SuspiciousOperation, BadRequest, MultiPartParserError)
    )
