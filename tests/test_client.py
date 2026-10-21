import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hub import FakeHub  # noqa: E402

from vitalwatch.client import MAX_BODY_BYTES, HubClient  # noqa: E402

SECRET = "s" * 64


class ClientTest(unittest.TestCase):
    def test_signature_verified_by_hub(self):
        with FakeHub(SECRET) as hub:
            client = HubClient(url=hub.url, key="project", secret=SECRET)
            result = client.send({"kind": "exception", "msg": "attempt"})

        self.assertTrue(result["sent"])
        self.assertEqual(204, result["status"])
        self.assertEqual(1, len(hub.requests))
        self.assertTrue(hub.requests[0]["accepted"], "hub imzayı kabul etmeli")
        self.assertEqual("/api/i/project/server", hub.requests[0]["path"])

    def test_wrong_secret_rejected_by_hub(self):
        with FakeHub(SECRET) as hub:
            # Gerçek arıza biçimi: secret eksik kopyalanmış. Gönderen taraf
            # başarılı görünür (204), hub sessizce atar.
            HubClient(url=hub.url, key="project", secret="s" * 63).send({"kind": "exception", "msg": "x"})

        self.assertFalse(hub.requests[0]["accepted"])

    def test_yapilandirma_eksikse_gonderilmez(self):
        result = HubClient(url=None, key=None, secret=None).send({"kind": "exception", "msg": "x"})

        self.assertEqual("missing-config", result["error"])

    def test_unreachable_hub_does_not_raise(self):
        # Kapalı port: bağlantı reddedilir.
        result = HubClient(url="http://127.0.0.1:9", key="k", secret=SECRET, timeout=0.5).send(
            {"kind": "exception", "msg": "x"}
        )

        self.assertFalse(result["sent"])
        self.assertIsNotNone(result["error"])

    def test_large_body_fits_by_dropping_stack(self):
        with FakeHub(SECRET) as hub:
            client = HubClient(url=hub.url, key="project", secret=SECRET)
            client.send({"kind": "exception", "msg": "collapsed", "stack": "y" * 20000})

        request = hub.requests[0]
        self.assertLessEqual(len(request["raw"]), MAX_BODY_BYTES)
        self.assertTrue(request["accepted"], "kırpılmış gövde hub sınırına sığmalı")
        self.assertNotIn("stack", request["body"])

    def test_redirect_not_followed(self):
        # İmzalı gövde bilinmeyen bir adrese gönderilmemeli.
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class Redirector(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "http://other.example/take")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address

        try:
            result = HubClient(url=f"http://{host}:{port}", key="k", secret=SECRET).send(
                {"kind": "exception", "msg": "x"}
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(302, result["status"])


class TimeoutTest(unittest.TestCase):
    """VITALWATCH_TIMEOUT: Node milisaniye bekliyor; aynı adla 2000 yazılınca süreç
    hub'a 2000 saniye bağlanırdı."""

    def test_seconds_and_milliseconds(self):
        from vitalwatch.client import timeout_seconds

        for input, expected in ((2, 2.0), ("2", 2.0), ("0.5", 0.5), (2000, 2.0),
                                ("1500", 1.5), (0, 2.0), ("abc", 2.0), (None, 2.0),
                                (-1, 2.0), ("nan", 2.0), ("inf", 2.0), (float("inf"), 2.0),
                                ("-inf", 2.0)):
            self.assertEqual(expected, timeout_seconds(input), input)

    def test_sinirlara_sikistirilir(self):
        # Ortak kural: sonuç [0.1, 10] saniye.
        from vitalwatch.client import timeout_seconds

        for input, expected in ((50, 10.0), (99, 10.0), (100, 0.1), (60000, 10.0),
                                ("0.01", 0.1), (10, 10.0), (0.1, 0.1)):
            self.assertEqual(expected, timeout_seconds(input), input)

if __name__ == "__main__":
    unittest.main()
