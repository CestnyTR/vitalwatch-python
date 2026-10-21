"""Süreç kancaları: tek kurulum, çağrı anında çözülen raporlayıcı, kurulum
sırasından bağımsız canlılık ve hiç fırlatmayan genel API."""

import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from flask import Flask
except ImportError:  # pragma: no cover
    Flask = None

from fake_hub import FakeHub  # noqa: E402

import vitalwatch  # noqa: E402
from vitalwatch import env as env_module  # noqa: E402

SECRET = "s" * 64


class _Isolated(unittest.TestCase):
    """Her test süreç kancalarını kurar; sonrasında her şey eski haline döner."""

    def setUp(self):
        self._saved = {
            "excepthook": sys.excepthook,
            "thread_excepthook": getattr(threading, "excepthook", None),
            "installed": vitalwatch._hooks_installed,
            "instance": vitalwatch._instance,
            "environ": {k: v for k, v in os.environ.items() if k.startswith("VITALWATCH_")},
            "cwd": os.getcwd(),
        }

        for key in self._saved["environ"]:
            del os.environ[key]

        self.directory = tempfile.mkdtemp()
        os.chdir(self.directory)
        env_module.forget()

        vitalwatch.stop_heartbeat()
        vitalwatch._hooks_installed = False
        vitalwatch._instance = None

        self.addCleanup(self._restore)

    def _restore(self):
        vitalwatch.stop_heartbeat()
        sys.excepthook = self._saved["excepthook"]

        if self._saved["thread_excepthook"] is not None:
            threading.excepthook = self._saved["thread_excepthook"]

        vitalwatch._hooks_installed = self._saved["installed"]
        vitalwatch._instance = self._saved["instance"]
        os.environ.update(self._saved["environ"])
        os.chdir(self._saved["cwd"])
        env_module.forget()


class HookProcessTest(_Isolated):
    def test_hooks_installed_once_per_process(self):
        with mock.patch.object(vitalwatch.atexit, "register") as register:
            vitalwatch.hook_process()
            first = sys.excepthook
            vitalwatch.hook_process()

        self.assertIs(first, sys.excepthook)
        self.assertEqual(1, register.call_count)

    def test_kanca_sonradan_init_edilen_raporlayiciyi_kullanir(self):
        vitalwatch.hook_process()  # yapılandırma yok: canlılık başlamaz

        self.assertIsNone(vitalwatch._heartbeat)

        with FakeHub(SECRET) as hub:
            new = vitalwatch.init(url=hub.url, key="new", secret=SECRET, timeout=2.0)

            # init, kancalar kuruluysa canlılığı yeniden dener.
            self.assertIsNotNone(vitalwatch._heartbeat)

            error = ValueError("süreç düştü")

            with redirect_stderr(io.StringIO()):
                sys.excepthook(ValueError, error, None)

            new.flush(5)

        kinds = [r["body"].get("kind") for r in hub.requests]
        self.assertIn("exception", kinds)
        self.assertTrue(all("/new/" in r["path"] for r in hub.requests))
        # Canlılık isteği (olay taşımayan toplu istek) de gitti.
        self.assertTrue(any(r["body"].get("events") == [] for r in hub.requests))

    def test_thread_kancasi_sonradan_init_edilen_raporlayiciyi_kullanir(self):
        vitalwatch.hook_process()

        with FakeHub(SECRET) as hub:
            new = vitalwatch.init(url=hub.url, key="new", secret=SECRET, timeout=2.0)

            def patla():
                raise RuntimeError("thread düştü")

            with redirect_stderr(io.StringIO()):
                worker = threading.Thread(target=patla)
                worker.start()
                worker.join()

            new.flush(5)

        self.assertIn("exception", [r["body"].get("kind") for r in hub.requests])


class NeverRaisesTest(_Isolated):
    def test_report_does_not_raise_even_if_setup_fails(self):
        with mock.patch.object(vitalwatch, "init", side_effect=RuntimeError("broken")):
            result = vitalwatch.report(ValueError("x"))

        self.assertFalse(result["sent"])

    def test_non_utf8_env_does_not_break_setup(self):
        Path(self.directory, ".env").write_bytes(b"VITALWATCH_ENV=local\nNOT=\xe7\xfc\n")

        self.assertIsNotNone(vitalwatch.reporter())
        self.assertFalse(vitalwatch.report(ValueError("x"))["sent"])


@unittest.skipIf(Flask is None, "flask kurulu değil")
class FlaskHooksTest(_Isolated):
    def test_vitalwatchflask_init_first_then_liveness_and_hook_on_new_instance(self):
        from vitalwatch.flask import VitalWatchFlask

        app = Flask(__name__)
        VitalWatchFlask(app)  # yapılandırma yok

        self.assertIsNone(vitalwatch._heartbeat)

        with FakeHub(SECRET) as hub:
            new = vitalwatch.init(url=hub.url, key="new", secret=SECRET, timeout=2.0)

            self.assertIsNotNone(vitalwatch._heartbeat)

            with redirect_stderr(io.StringIO()):
                sys.excepthook(ValueError, ValueError("düştü"), None)

            new.flush(5)

        self.assertIn("exception", [r["body"].get("kind") for r in hub.requests])

    def test_vitalwatchflask_does_not_raise_with_latin1_env(self):
        from vitalwatch.flask import VitalWatchFlask

        Path(self.directory, ".env").write_bytes(b"VITALWATCH_URL=http://x\nNOT=\xe7\xfc\n")

        app = Flask(__name__)
        VitalWatchFlask(app)

        self.assertIn("vitalwatch", app.extensions)

    def test_vitalwatchflask_does_not_chain_hooks_twice(self):
        from vitalwatch.flask import VitalWatchFlask

        app = Flask(__name__)
        VitalWatchFlask(app)
        first = sys.excepthook
        VitalWatchFlask(app)
        VitalWatchFlask(Flask("other"))

        self.assertIs(first, sys.excepthook)


if __name__ == "__main__":
    unittest.main()
