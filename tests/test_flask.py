import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from flask import Flask
except ImportError:  # pragma: no cover
    Flask = None

from fake_hub import FakeHub  # noqa: E402

SECRET = "s" * 64


@unittest.skipIf(Flask is None, "flask kurulu değil")
class FlaskTest(unittest.TestCase):
    def setUp(self):
        import vitalwatch

        self.hub = FakeHub(SECRET).__enter__()
        self.addCleanup(self.hub.__exit__, None, None, None)

        # Her test kendi raporlayıcısını kurar; modül düzeyindeki tekil
        # örnek testler arasında sızmasın.
        vitalwatch._instance = None
        self.reporter = vitalwatch.init(
            url=self.hub.url, key="project", secret=SECRET, slow_request_ms=100, timeout=2.0
        )

    def app(self):
        from vitalwatch.flask import VitalWatchFlask

        app = Flask(__name__)
        app.config["PROPAGATE_EXCEPTIONS"] = False

        @app.route("/urun/<int:urun_id>")
        def urun(urun_id):
            return {"id": urun_id}

        @app.route("/patlat")
        def patlat():
            raise RuntimeError("kasıtlı hata")

        # hook_process_errors kapalı: test sürecinin excepthook'unu değiştirmesin.
        VitalWatchFlask(app, hook_process_errors=False)

        return app

    def test_successful_request_not_reported(self):
        self.app().test_client().get("/urun/7")
        self.reporter.flush(3)

        self.assertEqual([], self.hub.requests)

    def test_uncaught_exception_reported_with_route_pattern(self):
        response = self.app().test_client().get("/patlat")
        self.reporter.flush(5)

        # Uygulamanın kendi davranışı korunur: hata yutulmaz, 500 döner.
        self.assertEqual(500, response.status_code)

        kinds = {request["body"]["kind"] for request in self.hub.requests}
        self.assertIn("exception", kinds)

        event = next(r["body"] for r in self.hub.requests if r["body"]["kind"] == "exception")
        self.assertEqual("kasıtlı hata", event["msg"])
        self.assertEqual("RuntimeError", event["exception_class"])
        self.assertEqual("/patlat", event["route"])
        self.assertTrue(all(r["accepted"] for r in self.hub.requests))

    def test_failing_request_creates_single_report(self):
        # Eskiden stack'li exception + stack'siz http_5xx: tek arıza iki kayıt.
        self.app().test_client().get("/patlat")
        self.reporter.flush(5)

        kinds = [request["body"]["kind"] for request in self.hub.requests]
        self.assertEqual(["exception"], kinds)

    def test_hatasiz_5xx_http_5xx_olarak_raporlanir(self):
        app = self.app()

        @app.route("/maintenance")
        def maintenance():
            return "bakımda", 503

        app.test_client().get("/maintenance")
        self.reporter.flush(5)

        kinds = [request["body"]["kind"] for request in self.hub.requests]
        self.assertEqual(["http_5xx"], kinds)

    def test_error_not_lost_with_exception_errorhandler(self):
        # Uygulama Exception için işleyici tanımlayınca teardown error=None
        # alıyordu; yalnızca stack'siz http_5xx gidiyor, hata kayboluyordu.
        app = self.app()

        @app.errorhandler(Exception)
        def all(e):
            return "özür dileriz", 500

        response = app.test_client().get("/patlat")
        self.reporter.flush(5)

        self.assertEqual(500, response.status_code)
        kinds = [request["body"]["kind"] for request in self.hub.requests]
        self.assertEqual(["exception"], kinds)
        self.assertEqual("RuntimeError", self.hub.requests[0]["body"]["exception_class"])

    def test_errorhandler_4xx_donerse_gonderilmez(self):
        app = self.app()

        @app.errorhandler(RuntimeError)
        def istemci(e):
            return "geçersiz", 400

        app.test_client().get("/patlat")
        self.reporter.flush(3)

        self.assertEqual([], self.hub.requests)

    def test_abort_4xx_gonderilmez(self):
        app = self.app()

        @app.route("/yasak")
        def yasak():
            from flask import abort

            abort(403)

        app.test_client().get("/yasak")
        app.test_client().get("/does-not-exist")
        self.reporter.flush(3)

        self.assertEqual([], self.hub.requests)

    def test_double_install_does_not_create_duplicate_reports(self):
        # after_request iki kez kaydediliyor, yavaş istek iki olay açıyordu.
        import time as _time

        from vitalwatch.flask import VitalWatchFlask

        app = self.app()
        VitalWatchFlask(app, hook_process_errors=False)

        @app.route("/slow")
        def slow():
            _time.sleep(0.15)
            return "ok"

        @app.route("/close")
        def close():
            from flask import abort

            abort(500)

        client = app.test_client()
        client.get("/slow")
        client.get("/close")
        self.reporter.flush(5)

        kinds = sorted(request["body"]["kind"] for request in self.hub.requests)
        self.assertEqual(["http_5xx", "slow_request"], kinds)

    def test_handler_returning_text_status_500_reported_with_stack(self):
        app = self.app()

        @app.errorhandler(RuntimeError)
        def text(e):
            return "özür", "500 SUNUCU HATASI"

        response = app.test_client().get("/patlat")
        self.reporter.flush(5)

        self.assertEqual(500, response.status_code)
        kinds = [request["body"]["kind"] for request in self.hub.requests]
        self.assertEqual(["exception"], kinds)

    def test_setup_error_does_not_crash_app(self):
        from unittest import mock

        from vitalwatch.flask import VitalWatchFlask

        app = Flask(__name__)

        with mock.patch.object(VitalWatchFlask, "_install", side_effect=RuntimeError("broken")):
            self.assertIs(app, VitalWatchFlask(hook_process_errors=False).init_app(app))

    def test_server_error_status_formats(self):
        from vitalwatch.flask import _server_error

        self.assertTrue(_server_error(("x", 500)))
        self.assertTrue(_server_error(("x", "503 BAKIM")))
        self.assertTrue(_server_error(("x", "500", {"X": "1"})))
        self.assertFalse(_server_error(("x", "404 YOK")))
        self.assertFalse(_server_error(("x", {"X": "1"})))
        self.assertFalse(_server_error(("x", "broken")))
        self.assertFalse(_server_error("düz gövde"))

    def test_sonradan_init_edilen_raporlayici_kullanilir(self):
        # Kancalar raporlayıcıyı kurulumda yakalıyordu; sonraki init'i görmüyordu.
        import vitalwatch

        app = self.app()
        new = vitalwatch.init(url=self.hub.url, key="new-project", secret=SECRET, timeout=2.0)

        app.test_client().get("/patlat")
        new.flush(5)

        self.assertTrue(self.hub.requests)
        self.assertTrue(all("/new-project/" in r["path"] for r in self.hub.requests))

    def test_route_pattern_sent_not_real_id(self):
        from vitalwatch.flask import route_pattern

        app = self.app()

        with app.test_request_context("/urun/42"):
            from flask import request

            self.assertEqual("/urun/<int:urun_id>", route_pattern(request))

    def test_unmatched_path_recorded_without_query_string(self):
        from vitalwatch.flask import route_pattern

        app = self.app()

        with app.test_request_context("/none?token=masked"):
            from flask import request

            self.assertEqual("/none", route_pattern(request))


if __name__ == "__main__":
    unittest.main()
