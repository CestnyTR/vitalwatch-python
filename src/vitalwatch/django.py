"""Django entegrasyonu: yavaş istek, 5xx ve yakalanmamış istisna ölçümü.

Kullanım (``settings.py``)::

    MIDDLEWARE = [
        "vitalwatch.django.VitalWatchMiddleware",  # listenin başına yakın
        ...
    ]

Süreç kancaları (``hook_process``) ilk kurulumda bir kez açılır; kapatmak için
``VITALWATCH_HOOK_PROCESS = False``.

Django bu modülün **içinde** import edilir; paketin kendisi Django'ya bağımlı
değildir ve Django kurulu olmayan bir projede ``import vitalwatch`` çalışır.

Hatayı iki yoldan görür, tekilleştirme istisnanın üzerindeki işaretle yapılır:

- ``got_request_exception`` sinyali: Django bir istisnayı **500'e çevirdiği**
  anda gönderir (``Http404``, ``PermissionDenied``, ``BadRequest``,
  ``SuspiciousOperation`` için göndermez). Görünümün yanında altımızdaki ara
  katmanlardan çıkan hataları da kapsar; ``process_exception`` onları görmez.
- ``process_exception``: yalnızca görünüm hatalarında çağrılır; istisna
  isteğe not edilir, yanıt 5xx çıkarsa ve sinyal kaçtıysa istek sonunda
  raporlanır. ``None`` döner — Django'nun kendi işleyişi aynen sürer.
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
    """Yeni tip Django ara katmanı (``get_response`` alır)."""

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
    """Django'nun eşleşen rota deseni (``/urun/<int:urun_id>/``), gerçek yol değil.

    ``re_path`` desenlerinde baştaki ``^`` ve sondaki ``$`` atılır. Desen
    yoksa (404, çözümleme öncesi hata) yola düşülür ve query string atılır (M3).
    Desen ``scrubber.path``'ten geçirilmez: ``(?P<id>...)`` içindeki ``?``
    query string sanılıp desen kesilirdi.
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
