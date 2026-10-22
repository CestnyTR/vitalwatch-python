"""Flask integration: slow requests, 5xx responses and uncaught exceptions.

Usage::

    from flask import Flask
    from vitalwatch.flask import VitalWatchFlask

    app = Flask(__name__)
    VitalWatchFlask(app)

Flask is imported **inside** this module only; the package itself does not
depend on Flask, and ``import vitalwatch`` keeps working in projects without it.

Hooks are installed on ``teardown_request`` and ``after_request``; ``blinker``
signals are not used — signal support varies between Flask versions and this
package has a zero-dependency policy.

The app's ``handle_user_exception`` / ``handle_exception`` methods are also
wrapped (per instance; the class is not touched). Reason: when the app defines
``@errorhandler(Exception)``, Flask treats the error as "handled", teardown
receives ``error=None`` and the error used to be **lost** — only a stackless
"HTTP 500" reached the panel. The wrapper sees the error and passes the return
value through unchanged.
"""

import time

from . import hook_process, reporter, scrubber

#: İstek başlangıcını ``flask.g`` üzerinde taşıyan alan adı.
_STARTED_AT = "_vitalwatch_started_at"

#: Bu istekte hata stack'iyle raporlandı; "HTTP 500" ayrıca açılmaz.
#: Node SDK'daki ``vitalwatch.reported`` işaretinin karşılığı.
_REPORTED = "_vitalwatch_reported"


class VitalWatchFlask:
    """Flask extension.

    :param app: The application. If omitted, call ``init_app`` later.
    :param hook_process_errors: Also install the uncaught-exception hooks and
        the heartbeat timer. On by default: a one-step setup is safer than a
        heartbeat call that has to be remembered separately.
    """

    def __init__(self, app=None, hook_process_errors=True):
        self.hook_process_errors = hook_process_errors

        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """Install the hooks. Never raises; a second call is a no-op.

        Installing twice used to register ``after_request`` twice, so every slow
        request and ``abort(500)`` produced two events; a marker is kept in
        ``app.extensions``.
        """
        try:
            extensions = getattr(app, "extensions", None)

            if isinstance(extensions, dict) and "vitalwatch" in extensions:
                self._hook_process()
                return app

            self._install(app)

            if isinstance(extensions, dict):
                extensions["vitalwatch"] = self
        except Exception:  # noqa: BLE001
            # İzleme kurulumu uygulamanın açılışını düşürmez.
            pass

        self._hook_process()

        return app

    def _hook_process(self):
        if not self.hook_process_errors:
            return

        try:
            hook_process()
        except Exception:  # noqa: BLE001
            pass

    def _install(self, app):
        from flask import g, request

        # Raporlayıcı her çağrıda çözülür, burada yakalanmaz: VitalWatchFlask(app)
        # sonrası vitalwatch.init(...) çağrılırsa kancalar eski örnekte kalıyordu.

        def report(error):
            try:
                current = reporter()

                if not current.configured():
                    return

                current.record_exception(
                    error,
                    route=route_pattern(request),
                    method=request.method,
                )
                setattr(g, _REPORTED, True)
            except Exception:  # noqa: BLE001
                pass

        @app.before_request
        def _vitalwatch_before():
            setattr(g, _STARTED_AT, time.perf_counter())

        @app.after_request
        def _vitalwatch_after(response):
            try:
                started = getattr(g, _STARTED_AT, None)
                status = response.status_code

                # Hata stack'iyle raporlandı; "HTTP 500" aynı arızanın tekrarı.
                if status >= 500 and getattr(g, _REPORTED, False):
                    return response

                if started is not None:
                    duration_ms = (time.perf_counter() - started) * 1000
                    current = reporter()

                    # Rota deseni yalnızca olay gerçekten gidecekse hesaplanır.
                    if current.wants_request(status, duration_ms):
                        current.record_request(
                            route=route_pattern(request),
                            method=request.method,
                            status=status,
                            duration_ms=duration_ms,
                        )
            except Exception:  # noqa: BLE001
                # İzleme kodu isteği bozmaz.
                pass

            return response

        @app.teardown_request
        def _vitalwatch_teardown(error=None):
            # Hata **yutulmaz**: teardown yalnızca bilgilendirilir, Flask'ın
            # kendi hata sayfası ve handler zinciri olduğu gibi çalışır.
            if error is None:
                return

            # Aynı hata handle_exception'da gönderildiyse raporlayıcı eler.
            report(error)

        original_user = app.handle_user_exception
        original_unhandled = app.handle_exception

        def handle_user_exception(e):
            # İşleyicisi yoksa yeniden fırlatır → handle_exception görür.
            response = original_user(e)

            try:
                if not _http_exception(e) and _server_error(response):
                    report(e)
            except Exception:  # noqa: BLE001
                pass

            return response

        def handle_exception(e):
            # Yakalanmamış hata; Flask her zaman 500'e çevirir.
            report(e)

            return original_unhandled(e)

        app.handle_user_exception = handle_user_exception
        app.handle_exception = handle_exception


def route_pattern(request):
    """Flask's matched route pattern (``/product/<id>``), not the actual path.

    This keeps grouping working — the same route collapses into one
    fingerprint — and keeps personal data in the path from being sent. Without
    a pattern (404) it falls back to the path with the query string removed (M3).
    """
    try:
        rule = request.url_rule.rule if request.url_rule is not None else None
    except Exception:  # noqa: BLE001
        rule = None

    return scrubber.path(rule or request.path) or "/"


def _http_exception(error):
    """werkzeug ``HTTPException``: ``abort(404)`` gibi bilinçli yanıt, arıza değil."""
    try:
        from werkzeug.exceptions import HTTPException
    except ImportError:  # pragma: no cover
        return False

    return isinstance(error, HTTPException)


def _server_error(response):
    """İşleyicinin döndürdüğü yanıt 5xx mi.

    Flask işleyiciden yanıt nesnesi, ``(gövde, durum)`` / ``(gövde, durum,
    başlıklar)`` demeti ya da düz gövde alabiliyor; düz gövde 200 demek. Durum
    sayı ya da ``"500 SUNUCU"`` gibi metin olabilir.
    """
    status = _status_code(getattr(response, "status_code", None))

    if status is None and isinstance(response, tuple) and len(response) >= 2:
        status = _status_code(response[1])

    return status is not None and status >= 500


def _status_code(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return int(value)

    if isinstance(value, str):
        try:
            return int(value.strip().split()[0])
        except (IndexError, ValueError):
            return None

    return None
