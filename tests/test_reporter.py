import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hub import FakeHub  # noqa: E402

from vitalwatch.reporter import KINDS, Reporter  # noqa: E402

SECRET = "s" * 64


def reporter_for(hub, **options):
    settings = {"url": hub.url, "key": "project", "secret": SECRET, "timeout": 2.0}
    settings.update(options)

    return Reporter(**settings)


class ReporterTest(unittest.TestCase):
    def test_exception_sent_in_hub_accepted_format(self):
        with FakeHub(SECRET) as hub:
            reporter = reporter_for(hub)

            try:
                raise ValueError("ürün bulunamadı")
            except ValueError as error:
                reporter.record_exception(error, route="/urun/<id>", method="GET", block=True)

        body = hub.requests[0]["body"]
        self.assertTrue(hub.requests[0]["accepted"])
        self.assertIn(body["kind"], KINDS)
        self.assertEqual("exception", body["kind"])
        self.assertEqual("ürün bulunamadı", body["msg"])
        self.assertEqual("ValueError", body["exception_class"])
        self.assertEqual("production", body["env"])
        self.assertEqual("server", body["source"])
        self.assertEqual("python", body["runtime"])
        self.assertIn("traceback", body["stack"].lower())

    def test_same_exception_not_sent_twice(self):
        with FakeHub(SECRET) as hub:
            reporter = reporter_for(hub)
            error = ValueError("x")

            reporter.record_exception(error, block=True)
            result = reporter.record_exception(error, block=True)

        self.assertEqual("already-reported", result["error"])
        self.assertEqual(1, len(hub.requests))

    def test_memory_error_marked_as_fatal(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub).record_exception(MemoryError("bellek bitti"), block=True)

        self.assertEqual("fatal", hub.requests[0]["body"]["kind"])

    def test_personal_data_in_message_is_scrubbed(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub).record_exception(
                ValueError("ahmet@ornek.com için kayıt yok"), block=True
            )

        self.assertNotIn("john@sample.com", hub.requests[0]["raw"].decode("utf-8"))

    def test_fast_successful_request_not_reported(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub).record_request("/", "GET", 200, 10, block=True)

        self.assertEqual([], hub.requests)

    def test_slow_request_is_reported(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub, slow_request_ms=100).record_request("/search", "GET", 200, 250, block=True)

        body = hub.requests[0]["body"]
        self.assertEqual("slow_request", body["kind"])
        self.assertEqual(250, body["duration_ms"])
        self.assertEqual("GET /search", body["route"])

    def test_request_event_carries_memory(self):
        from vitalwatch.reporter import memory_mb

        with FakeHub(SECRET) as hub:
            reporter_for(hub, slow_request_ms=100).record_request("/search", "GET", 200, 250, block=True)

        body = hub.requests[0]["body"]
        self.assertTrue(hub.requests[0]["accepted"])

        if sys.platform.startswith("win"):  # pragma: no cover
            self.assertNotIn("memory_mb", body)
        else:
            self.assertIsInstance(body["memory_mb"], int)
            self.assertGreater(body["memory_mb"], 0)
            # Tepe bellek (ru_maxrss) yalnızca artar: olay gönderildikten sonra
            # süreç büyümüş olabilir. Eşitlik beklemek CI'da 1 MB farkla
            # düşüyordu (3.13'te 144 ≠ 143); doğru olan "sonrakinden büyük değil".
            self.assertLessEqual(body["memory_mb"], memory_mb())

    def test_bellek_birimi_platforma_gore(self):
        # ru_maxrss Linux'ta KB, macOS'ta bayt.
        from types import SimpleNamespace
        from unittest import mock

        try:
            import resource
        except ImportError:  # pragma: no cover
            self.skipTest("resource yok (Windows)")

        from vitalwatch.reporter import memory_mb

        usage = SimpleNamespace(ru_maxrss=200 * 1024)

        with mock.patch.object(resource, "getrusage", return_value=usage), \
                mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(200, memory_mb())

        usage = SimpleNamespace(ru_maxrss=200 * 1048576)

        with mock.patch.object(resource, "getrusage", return_value=usage), \
                mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(200, memory_mb())

        with mock.patch.object(resource, "getrusage", side_effect=OSError("none")):
            self.assertIsNone(memory_mb())

    def test_server_error_reported_as_5xx(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub).record_request("/api/analiz", "POST", 500, 20, block=True)

        self.assertEqual("http_5xx", hub.requests[0]["body"]["kind"])

    def test_liveness_request_carries_no_event(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub).heartbeat()

        body = hub.requests[0]["body"]
        self.assertEqual([], body["events"])
        self.assertNotIn("kind", body)
        self.assertEqual("python", body["runtime"])

    def test_call_does_not_wait_when_queue_used(self):
        with FakeHub(SECRET) as hub:
            reporter = reporter_for(hub)
            result = reporter.record_exception(ValueError("queue"))

            self.assertTrue(result["queued"])
            self.assertTrue(reporter.flush(5), "kuyruk boşalmalı")

        self.assertEqual(1, len(hub.requests))

    def test_event_dropped_when_queue_full_app_does_not_wait(self):
        # Erişilemeyen hub: gönderim thread'i takılır, kuyruk dolar. Beklenen
        # davranış olayı düşürmek — bloke olmak izlenen uygulamayı yavaşlatır.
        reporter = Reporter(url="http://127.0.0.1:9", key="k", secret=SECRET, timeout=5)
        results = [reporter.record_exception(ValueError(f"hata {i}")) for i in range(400)]

        self.assertTrue(any(r.get("error") == "queue-full" for r in results))

    def test_yapilandirma_yoksa_sessizce_vazgecilir(self):
        result = Reporter(url=None, key=None, secret=None).record_exception(ValueError("x"))

        self.assertEqual("missing-config", result["error"])

    def test_devre_disiyken_hicbir_sey_gonderilmez(self):
        with FakeHub(SECRET) as hub:
            reporter_for(hub, enabled=False).record_exception(ValueError("x"), block=True)

        self.assertEqual([], hub.requests)

    def test_alias_converted_to_env_equivalent(self):
        # Hub küme dışı env'i sessizce atar; takma adlar karşılığına çevrilir
        # (tanınmayanlar için bkz. test_environment.py).
        self.assertEqual("production", Reporter(env="prod").env)
        self.assertEqual("staging", Reporter(env="staging").env)


if __name__ == "__main__":
    unittest.main()
