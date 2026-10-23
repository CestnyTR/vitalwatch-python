"""Testler için sahte hub: hub'ın imza doğrulamasını birebir uygular.

Gerçek hub geçersiz isteğe de 204 döner. Burada da 204 dönülür ama istek
**kaydedilir**: testin görmesi gereken şey yanıt değil, hub'ın gövdeyi kabul
edip etmeyeceği.
"""

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY_BYTES = 8192
#: /deps ucunun sınırı (hub: 512 KB).
MAX_DEPENDENCIES_BODY_BYTES = 512 * 1024
MAX_AGE_SECONDS = 300


class FakeHub:
    def __init__(self, secret):
        self.secret = secret
        self.requests = []
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self._server.server_address

        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()

        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()

    def _handler(hub):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)

                record = {
                    "path": self.path,
                    "raw": raw,
                    "accepted": hub._accepts(self.headers, raw, self.path),
                    "body": None,
                }

                try:
                    record["body"] = json.loads(raw.decode("utf-8"))
                except ValueError:
                    pass

                hub.requests.append(record)

                # Hub her koşulda 204 döner — başarıda da, geçersiz istekte de.
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        return Handler

    def _accepts(self, headers, raw, path=""):
        """vitalwatch-hub VerifyProjectSignature + IngestController sınırları."""
        signature = headers.get("X-VitalWatch-Signature")
        timestamp = headers.get("X-VitalWatch-Timestamp")

        if not signature or not timestamp or not timestamp.isdigit():
            return False

        if abs(int(time.time()) - int(timestamp)) > MAX_AGE_SECONDS:
            return False

        limit = MAX_DEPENDENCIES_BODY_BYTES if path.endswith("/deps") else MAX_BODY_BYTES

        if len(raw) > limit:
            return False

        expected = "sha256=" + hmac.new(
            self.secret.encode("utf-8"),
            f"{timestamp}.".encode("utf-8") + raw,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)
