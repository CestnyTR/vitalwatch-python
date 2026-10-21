import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse, PlainTextResponse
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    FastAPI = None

from fake_hub import FakeHub  # noqa: E402

SECRET = "s" * 64


class _Base(unittest.TestCase):
    def setUp(self):
        import vitalwatch

        self.hub = FakeHub(SECRET).__enter__()
        self.addCleanup(self.hub.__exit__, None, None, None)

        vitalwatch._instance = None
        self.reporter = vitalwatch.init(
            url=self.hub.url, key="project", secret=SECRET, slow_request_ms=100, timeout=2.0
        )

    def events(self):
        self.reporter.flush(5)

        return [request["body"] for request in self.hub.requests]

    def kinds(self):
        return [event["kind"] for event in self.events()]


class RawAsgiTest(_Base):
    """Çerçevesiz ASGI: Starlette/FastAPI kurulu olmasa da çalışmalı."""

    def run_app(self, app, path="/raw", scope_type="http"):
        from vitalwatch.asgi import VitalWatchASGIMiddleware

        wrapped = VitalWatchASGIMiddleware(app, hook_process_errors=False)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": scope_type, "method": "GET", "path": path, "root_path": ""}
        asyncio.run(wrapped(scope, receive, send))

        return sent

    def test_exception_reraised_and_creates_single_report(self):
        async def app(scope, receive, send):
            raise RuntimeError("ham hata")

        with self.assertRaises(RuntimeError):
            self.run_app(app, path="/raw?token=masked")

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("/raw", events[0]["route"])
        self.assertEqual("GET", events[0]["method"])

    def test_exception_after_response_started_does_not_create_http_5xx(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 500, "headers": []})
            raise RuntimeError("yarıda")

        with self.assertRaises(RuntimeError):
            self.run_app(app)

        self.assertEqual(["exception"], self.kinds())

    def test_5xx_without_exception_reported_response_unchanged(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 502, "headers": []})
            await send({"type": "http.response.body", "body": b"x"})

        sent = self.run_app(app)

        self.assertEqual(502, sent[0]["status"])
        self.assertEqual(b"x", sent[1]["body"])
        self.assertEqual(["http_5xx"], self.kinds())

    def test_4xx_http_exception_gonderilmez(self):
        class HTTPException(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        async def app(scope, receive, send):
            raise HTTPException(404)

        with self.assertRaises(HTTPException):
            self.run_app(app)

        self.assertEqual([], self.events())

    def test_original_error_raised_if_client_error_check_fails(self):
        class Weird(Exception):
            @property
            def status_code(self):
                raise KeyError("özellik patladı")

        async def app(scope, receive, send):
            raise Weird("özgün")

        with self.assertRaises(Weird):
            self.run_app(app)

    def test_nested_middleware_single_report(self):
        from vitalwatch.asgi import VitalWatchASGIMiddleware

        async def app(scope, receive, send):
            await asyncio.sleep(0.15)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x"})

        inner = VitalWatchASGIMiddleware(app, hook_process_errors=False)
        self.run_app(inner, path="/ic")

        self.assertEqual(["slow_request"], self.kinds())

    def test_non_http_scope_not_measured(self):
        async def app(scope, receive, send):
            raise RuntimeError("lifespan")

        with self.assertRaises(RuntimeError):
            self.run_app(app, scope_type="lifespan")

        self.assertEqual([], self.events())


@unittest.skipIf(FastAPI is None, "fastapi/starlette kurulu değil")
class FastAPITest(_Base):
    def app(self):
        from vitalwatch.asgi import VitalWatchASGIMiddleware

        app = FastAPI()

        @app.get("/urun/{urun_id}")
        def urun(urun_id: int):
            return {"id": urun_id}

        @app.get("/urun/{urun_id}/patlat")
        def urun_patlat(urun_id: int):
            raise RuntimeError("kasıtlı hata")

        @app.get("/patlat-async")
        async def patlat_async():
            raise RuntimeError("async hata")

        @app.get("/maintenance")
        def maintenance():
            return JSONResponse({"status": "bakım"}, status_code=503)

        @app.get("/yasak")
        def yasak():
            raise HTTPException(status_code=403)

        @app.get("/bulunamadi")
        def bulunamadi():
            raise HTTPException(status_code=404)

        @app.get("/value")
        def value():
            raise ValueError("geçersiz değer")

        app.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)

        return app

    def client(self, app, raise_server_exceptions=False):
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    def test_successful_request_not_reported(self):
        response = self.client(self.app()).get("/urun/7")

        self.assertEqual(200, response.status_code)
        self.assertEqual([], self.events())

    def test_uncaught_exception_reported_once_with_route_pattern(self):
        response = self.client(self.app()).get("/urun/42/patlat")

        self.assertEqual(500, response.status_code)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("RuntimeError", events[0]["exception_class"])
        self.assertEqual("/urun/{urun_id}/patlat", events[0]["route"])
        self.assertEqual("GET", events[0]["method"])
        self.assertTrue(all(r["accepted"] for r in self.hub.requests))

    def test_async_endpoint_exception_is_reported(self):
        self.client(self.app()).get("/patlat-async")

        self.assertEqual(["exception"], self.kinds())

    def test_exception_not_swallowed(self):
        # ServerErrorMiddleware 500'ü yazıp hatayı yeniden fırlatıyor; bizim
        # katmanımız araya girdiğinde de hata dışarı ulaşmalı.
        with self.assertRaises(RuntimeError):
            self.client(self.app(), raise_server_exceptions=True).get("/urun/1/patlat")

    def test_hatasiz_5xx_http_5xx_olarak_raporlanir(self):
        response = self.client(self.app()).get("/maintenance")

        self.assertEqual(503, response.status_code)

        events = self.events()
        self.assertEqual(["http_5xx"], [event["kind"] for event in events])
        self.assertEqual("GET /maintenance", events[0]["route"])

    def test_4xx_gonderilmez(self):
        client = self.client(self.app())

        self.assertEqual(403, client.get("/yasak").status_code)
        self.assertEqual(404, client.get("/bulunamadi").status_code)
        self.assertEqual(404, client.get("/does-not-exist?token=masked").status_code)
        self.assertEqual(422, client.get("/urun/abc").status_code)

        self.assertEqual([], self.events())

    def test_http_exception_5xx_stacksiz_http_5xx_acar(self):
        # Bilinçli HTTPException(503) arıza stack'i değil; Flask'taki abort(503)
        # gibi yalnızca http_5xx olarak düşer.
        app = self.app()

        @app.get("/closed")
        def closed():
            raise HTTPException(status_code=503)

        response = self.client(app).get("/closed")

        self.assertEqual(503, response.status_code)
        self.assertEqual(["http_5xx"], self.kinds())

    def test_exception_handler_exception_500_single_report(self):
        # Exception/500 işleyicisi ServerErrorMiddleware'e bağlanır; hata yine
        # bizim katmanımızdan geçer.
        app = self.app()

        @app.exception_handler(Exception)
        async def all(request, exc):
            return PlainTextResponse("özür dileriz", status_code=500)

        response = self.client(app).get("/urun/3/patlat")

        self.assertEqual(500, response.status_code)
        self.assertEqual("özür dileriz", response.text)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("RuntimeError", events[0]["exception_class"])

    def test_class_bound_handler_returning_5xx_reported_with_stack(self):
        # ValueError işleyicisi içteki ExceptionMiddleware'de çalışır, hata bize
        # ulaşmaz; sarmalayıcı olmadan yalnızca stack'siz http_5xx giderdi.
        app = self.app()

        @app.exception_handler(ValueError)
        def value_error(request, exc):
            return PlainTextResponse("server", status_code=500)

        response = self.client(app).get("/value")

        self.assertEqual(500, response.status_code)
        self.assertEqual("server", response.text)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("ValueError", events[0]["exception_class"])
        self.assertEqual("/value", events[0]["route"])

    def test_class_bound_handler_returning_4xx_not_sent(self):
        app = self.app()

        @app.exception_handler(ValueError)
        async def value_error(request, exc):
            return PlainTextResponse("geçersiz", status_code=400)

        response = self.client(app).get("/value")

        self.assertEqual(400, response.status_code)
        self.assertEqual([], self.events())

    def test_lifespan_gecer(self):
        with self.client(self.app()) as client:
            self.assertEqual(200, client.get("/urun/1").status_code)

        self.assertEqual([], self.events())

    def test_router_prefix_and_mount_added_to_pattern(self):
        from fastapi import APIRouter

        from vitalwatch.asgi import VitalWatchASGIMiddleware

        router = APIRouter(prefix="/v1")

        @router.get("/siparis/{siparis_id}")
        def siparis(siparis_id: int):
            raise RuntimeError("router")

        sub = FastAPI()
        sub.include_router(router)

        app = FastAPI()
        app.mount("/api", sub)
        app.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)

        self.client(app).get("/api/v1/siparis/9")

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("/api/v1/siparis/{siparis_id}", events[0]["route"])

    def _with_value_handler(self, app):
        @app.exception_handler(ValueError)
        def value_error(request, exc):
            return PlainTextResponse("server", status_code=500)

        return app

    def test_handler_wrapping_independent_of_middleware_order(self):
        # VitalWatch'tan sonra eklenen katman VitalWatch ile ExceptionMiddleware arasına
        # değil, dışına girer; ama önce eklenen (GZip) araya girer. İkisi de çalışmalı.
        from starlette.middleware.cors import CORSMiddleware
        from starlette.middleware.gzip import GZipMiddleware

        from vitalwatch.asgi import VitalWatchASGIMiddleware

        for later in (GZipMiddleware, CORSMiddleware):
            self.hub.requests.clear()
            app = FastAPI()
            self._with_value_handler(app)

            @app.get("/value")
            def value():
                raise ValueError("geçersiz")

            # Önce VitalWatch'ın içine girecek katman, sonra VitalWatch.
            app.add_middleware(later, **({"allow_origins": ["*"]} if later is CORSMiddleware else {}))
            app.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)

            self.assertEqual(500, self.client(app).get("/value").status_code)
            self.assertEqual(["exception"], self.kinds(), later.__name__)

    def test_manually_wrapped_app_wraps_handler_after_first_request(self):
        from vitalwatch.asgi import VitalWatchASGIMiddleware

        app = FastAPI()
        self._with_value_handler(app)

        @app.get("/value")
        def value():
            raise ValueError("geçersiz")

        client = self.client(VitalWatchASGIMiddleware(app, hook_process_errors=False))
        client.get("/value")
        self.reporter.flush(5)
        self.hub.requests.clear()

        client.get("/value")

        self.assertEqual(["exception"], self.kinds())

    def test_background_task_not_counted_as_slow_request(self):
        import time as _time

        from fastapi import BackgroundTasks

        app = self.app()

        @app.get("/back")
        def back(tasks: BackgroundTasks):
            tasks.add_task(_time.sleep, 0.3)
            return {"ok": True}

        self.assertEqual(200, self.client(app).get("/back").status_code)
        self.assertEqual([], self.events())

    def test_middleware_in_mounted_sub_app_does_not_create_duplicate_reports(self):
        import time as _time

        from vitalwatch.asgi import VitalWatchASGIMiddleware

        sub = FastAPI()

        @sub.get("/slow/{code}")
        def slow(code: str):
            _time.sleep(0.15)
            return {"code": code}

        sub.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)

        app = FastAPI()
        app.mount("/alt", sub)
        app.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)

        self.client(app).get("/alt/slow/abc")

        events = self.events()
        self.assertEqual(["slow_request"], [event["kind"] for event in events])
        self.assertEqual("GET /alt/slow/{code}", events[0]["route"])

    def test_route_pattern_not_computed_for_unsent_request(self):
        from unittest import mock

        import vitalwatch.asgi

        with mock.patch.object(vitalwatch.asgi, "route_pattern", wraps=vitalwatch.asgi.route_pattern) as spy:
            client = self.client(self.app())
            client.get("/urun/1")
            self.assertEqual(0, spy.call_count)

            client.get("/maintenance")
            self.assertEqual(1, spy.call_count)

    def test_sonradan_init_edilen_raporlayici_kullanilir(self):
        import vitalwatch

        client = self.client(self.app())
        new = vitalwatch.init(url=self.hub.url, key="new-project", secret=SECRET, timeout=2.0)

        client.get("/urun/1/patlat")
        new.flush(5)

        self.assertTrue(self.hub.requests)
        self.assertTrue(all("/new-project/" in r["path"] for r in self.hub.requests))


@unittest.skipIf(FastAPI is None, "fastapi/starlette kurulu değil")
class StarletteTest(_Base):
    def app(self):
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount, Route

        from vitalwatch.asgi import VitalWatchASGIMiddleware

        def urun_patlat(request):
            raise RuntimeError("starlette hatası")

        def maintenance(request):
            return PlainTextResponse("bakım", status_code=503)

        return Starlette(
            routes=[
                Route("/urun/{urun_id:int}/patlat", urun_patlat),
                Mount("/management", routes=[Route("/entry/{code}", urun_patlat)]),
                Route("/maintenance/{section}", maintenance),
            ],
            middleware=[Middleware(VitalWatchASGIMiddleware, hook_process_errors=False)],
        )

    def test_exception_single_report_with_route_pattern(self):
        response = TestClient(self.app(), raise_server_exceptions=False).get("/urun/5/patlat")

        self.assertEqual(500, response.status_code)

        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("/urun/{urun_id:int}/patlat", events[0]["route"])

    def test_pattern_under_mount(self):
        TestClient(self.app(), raise_server_exceptions=False).get("/management/entry/abc")

        events = self.events()
        self.assertEqual("/management/entry/{code}", events[0]["route"])

    def test_hatasiz_5xx_desenle_raporlanir(self):
        TestClient(self.app()).get("/maintenance/repository")

        events = self.events()
        self.assertEqual(["http_5xx"], [event["kind"] for event in events])
        self.assertEqual("GET /maintenance/{section}", events[0]["route"])

    def test_handler_wrapped_when_vitalwatch_outside_middleware_list(self):
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware
        from starlette.routing import Route

        from vitalwatch.asgi import VitalWatchASGIMiddleware

        def value(request):
            raise ValueError("geçersiz")

        def isleyici(request, exc):
            return PlainTextResponse("server", status_code=500)

        app = Starlette(
            routes=[Route("/value/{code}", value)],
            middleware=[
                Middleware(VitalWatchASGIMiddleware, hook_process_errors=False),
                Middleware(CORSMiddleware, allow_origins=["*"]),
            ],
            exception_handlers={ValueError: isleyici},
        )

        response = TestClient(app).get("/value/x")

        self.assertEqual(500, response.status_code)
        events = self.events()
        self.assertEqual(["exception"], [event["kind"] for event in events])
        self.assertEqual("/value/{code}", events[0]["route"])

    def test_starlette_deseni_onbellekten_gelir(self):
        from unittest import mock

        import vitalwatch.asgi

        vitalwatch.asgi._CACHE.clear()
        client = TestClient(self.app())

        with mock.patch("starlette.routing.compile_path", wraps=__import__("starlette.routing", fromlist=["x"]).compile_path) as spy:
            client.get("/maintenance/a")
            first = spy.call_count
            client.get("/maintenance/b")

        # Starlette 1.x scope["route"]'u kendisi koyuyor; desen oradan okunur ve
        # önbellek yoluna hiç girilmez. Eski sürümlerde ilk istek derler, ikincisi
        # önbellekten gelir. İki durumda da ikinci istek yeniden derlememeli.
        self.assertEqual(first, spy.call_count)
        self.assertEqual(["GET /maintenance/{section}"] * 2, [e["route"] for e in self.events()])

    def test_exception_not_swallowed(self):
        with self.assertRaises(RuntimeError):
            TestClient(self.app()).get("/urun/5/patlat")


if __name__ == "__main__":
    unittest.main()
