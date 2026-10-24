import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import django
    from django.conf import settings
except ImportError:  # pragma: no cover
    django = None

from fake_hub import FakeHub  # noqa: E402

SECRET = "s" * 64

urlpatterns = []

if django is not None:
    from django.core.exceptions import PermissionDenied
    from django.http import Http404, HttpResponse
    from django.urls import path, re_path

    def urun(request, urun_id):
        return HttpResponse(str(urun_id))

    def urun_patlat(request, urun_id):
        raise RuntimeError("kasıtlı hata")

    def maintenance(request):
        return HttpResponse("bakımda", status=503)

    def yasak(request):
        raise PermissionDenied()

    def bulunamadi(request):
        raise Http404()

    def old(request, code):
        raise ValueError("eski rota")

    urlpatterns = [
        path("urun/<int:urun_id>/", urun),
        path("urun/<int:urun_id>/patlat/", urun_patlat),
        path("maintenance/", maintenance),
        path("yasak/", yasak),
        path("bulunamadi/", bulunamadi),
        path("middleware/", urun, {"urun_id": 1}),
        re_path(r"^old/(?P<code>\d+)/$", old),
    ]

    class ExplodingMiddleware:
        """Bizim altımızda patlayan ara katman: process_exception onu görmez."""

        def __init__(self, get_response):
            self.get_response = get_response

        def __call__(self, request):
            if request.path == "/middleware/":
                raise RuntimeError("ara katman hatası")

            return self.get_response(request)

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            SECRET_KEY="test",
            ALLOWED_HOSTS=["testserver"],
            ROOT_URLCONF=__name__,
            INSTALLED_APPS=[],
            DATABASES={},
            TEMPLATES=[],
            MIDDLEWARE=[
                "vitalwatch.django.VitalWatchMiddleware",
                f"{__name__}.ExplodingMiddleware",
            ],
            # Test sürecinin excepthook'u değişmesin.
            VITALWATCH_HOOK_PROCESS=False,
            # Django 500'leri stderr'e basmasın.
            LOGGING={
                "version": 1,
                "disable_existing_loggers": False,
                "loggers": {"django": {"handlers": [], "level": "CRITICAL", "propagate": False}},
            },
        )
        django.setup()


@unittest.skipIf(django is None, "django kurulu değil")
class DjangoTest(unittest.TestCase):
    def setUp(self):
        import vitalwatch

        self.hub = FakeHub(SECRET).__enter__()
        self.addCleanup(self.hub.__exit__, None, None, None)

        vitalwatch._instance = None
        self.reporter = vitalwatch.init(
            url=self.hub.url, key="project", secret=SECRET,
            # Bu dosyada yavaş istek denenmiyor; eşik düşük olunca yavaş CI makinesinde
            # ilk istek (soğuk başlangıç) eşiği geçip olay listelerine sızıyordu
            # (24 Ekim 2026, Python 3.14 koşusu: "GET /yasak/ — 120 ms").
            slow_request_ms=60_000, timeout=2.0
        )

    def client(self):
        from django.test import Client

        # Test istemcisi sunucu hatasını varsayılan olarak yeniden fırlatır;
        # gerçek sunucudaki gibi 500 sayfası görülsün.
        return Client(raise_request_exception=False)

    def events(self):
        self.reporter.flush(5)

        return [request["body"] for request in self.hub.requests]

    def test_successful_request_not_reported(self):
        response = self.client().get("/urun/7/")

        self.assertEqual(200, response.status_code)
        self.assertEqual([], self.events())

    def test_route_pattern_not_computed_for_unsent_request(self):
        from unittest import mock

        import vitalwatch.django

        with mock.patch.object(vitalwatch.django, "route_pattern", wraps=vitalwatch.django.route_pattern) as spy:
            self.client().get("/urun/7/")
            self.assertEqual(0, spy.call_count)

            self.client().get("/maintenance/")
            self.assertEqual(1, spy.call_count)

    def test_uncaught_exception_reported_once_with_route_pattern(self):
        response = self.client().get("/urun/42/patlat/")

        # Uygulamanın kendi davranışı korunur: hata yutulmaz, 500 sayfası döner.
        self.assertEqual(500, response.status_code)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("RuntimeError", events[0]["exception_class"])
        self.assertEqual("kasıtlı hata", events[0]["msg"])
        # Gerçek id değil, desen.
        self.assertEqual("/urun/<int:urun_id>/patlat/", events[0]["route"])
        self.assertEqual("GET", events[0]["method"])
        self.assertIn("urun_patlat", events[0]["stack"])
        self.assertTrue(all(r["accepted"] for r in self.hub.requests))

    def test_exception_reraised_in_test_client(self):
        # Hata yutulmuyor: Django'nun kendi zinciri (sinyal → istemci) çalışıyor.
        from django.test import Client

        with self.assertRaises(RuntimeError):
            Client().get("/urun/1/patlat/")

    def test_inner_middleware_error_also_reported_with_stack(self):
        response = self.client().get("/middleware/")

        self.assertEqual(500, response.status_code)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("ara katman hatası", events[0]["msg"])

    def test_hatasiz_5xx_http_5xx_olarak_raporlanir(self):
        response = self.client().get("/maintenance/")

        self.assertEqual(503, response.status_code)

        events = self.events()
        self.assertEqual(["http_5xx"], [event["kind"] for event in events])
        self.assertEqual("GET /maintenance/", events[0]["route"])
        self.assertEqual(503, events[0]["status"])

    def test_4xx_gonderilmez(self):
        client = self.client()

        self.assertEqual(403, client.get("/yasak/").status_code)
        self.assertEqual(404, client.get("/bulunamadi/").status_code)
        self.assertEqual(404, client.get("/does-not-exist/?token=masked").status_code)

        self.assertEqual([], self.events())

    def test_process_exception_hatayi_yutmaz_ve_4xx_not_etmez(self):
        from django.http import Http404
        from django.test import RequestFactory

        from vitalwatch.django import VitalWatchMiddleware

        middleware = VitalWatchMiddleware(lambda request: None)
        request = RequestFactory().get("/")

        self.assertIsNone(middleware.process_exception(request, Http404()))
        self.assertFalse(hasattr(request, "_vitalwatch_exception"))

        error = RuntimeError("x")
        self.assertIsNone(middleware.process_exception(request, error))
        self.assertIs(error, request._vitalwatch_exception)

    def test_noted_exception_reported_if_signal_missed(self):
        # got_request_exception bir şekilde tetiklenmezse process_exception'ın
        # notu istek sonunda, yanıt 5xx ise, tek kayıt olarak gönderilir.
        from django.http import HttpResponse
        from django.test import RequestFactory

        from vitalwatch.django import VitalWatchMiddleware

        def view(request):
            middleware.process_exception(request, RuntimeError("notlu"))

            return HttpResponse(status=500)

        middleware = VitalWatchMiddleware(view)
        middleware(RequestFactory().get("/x"))

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("notlu", events[0]["msg"])

    def test_re_path_deseni_temizlenir(self):
        response = self.client().get("/old/123/")

        self.assertEqual(500, response.status_code)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("/old/(?P<code>\\d+)/", events[0]["route"])

    def test_sonradan_init_edilen_raporlayici_kullanilir(self):
        import vitalwatch

        client = self.client()
        new = vitalwatch.init(url=self.hub.url, key="new-project", secret=SECRET, timeout=2.0)

        client.get("/urun/1/patlat/")
        new.flush(5)

        self.assertTrue(self.hub.requests)
        self.assertTrue(all("/new-project/" in r["path"] for r in self.hub.requests))

    def test_unmatched_path_recorded_without_query_string(self):
        from django.test import RequestFactory

        from vitalwatch.django import route_pattern

        self.assertEqual("/none", route_pattern(RequestFactory().get("/none?token=masked")))


if __name__ == "__main__":
    unittest.main()
