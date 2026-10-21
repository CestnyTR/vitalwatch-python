import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch import env as env_module  # noqa: E402


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.directory = tempfile.mkdtemp()
        os.chdir(self.directory)
        env_module.forget()

        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self.previous_cwd)
        env_module.forget()

        for key in ("VITALWATCH_URL", "VITALWATCH_KEY", "APP_ENV"):
            os.environ.pop(key, None)

    def write(self, name, content):
        Path(self.directory, name).write_text(content, encoding="utf-8")

    def test_env_file_is_read(self):
        # Asıl gerekçe: vitalwatch-status ayrı bir süreç, uygulamanın .env'i
        # yüklenmez ve yalnızca os.environ'a bakan teşhis "TANIMSIZ" derdi.
        self.write(".env", "VITALWATCH_URL=https://hub.sample.com\nVITALWATCH_KEY=lens\n")

        values = env_module.env()

        self.assertEqual("https://hub.sample.com", values["VITALWATCH_URL"])
        self.assertEqual("lens", values["VITALWATCH_KEY"])

    def test_process_env_beats_file(self):
        self.write(".env", "VITALWATCH_KEY=dosyadan\n")
        os.environ["VITALWATCH_KEY"] = "ortamdan"

        self.assertEqual("ortamdan", env_module.env()["VITALWATCH_KEY"])

    def test_env_specific_file_comes_first(self):
        os.environ["APP_ENV"] = "staging"
        self.write(".env", "VITALWATCH_KEY=general\n")
        self.write(".env.staging", "VITALWATCH_KEY=staging\n")

        self.assertEqual("staging", env_module.env()["VITALWATCH_KEY"])

    def test_tirnak_yorum_ve_export_ayristirilir(self):
        self.write(
            ".env",
            "# yorum\nexport VITALWATCH_KEY='lens'\nVITALWATCH_URL=\"https://hub.sample.com\"\n"
            "VITALWATCH_ENV=production # satır sonu yorumu\n",
        )

        values = env_module.env()

        self.assertEqual("lens", values["VITALWATCH_KEY"])
        self.assertEqual("https://hub.sample.com", values["VITALWATCH_URL"])
        self.assertEqual("production", values["VITALWATCH_ENV"])

    def test_non_vitalwatch_variables_not_read(self):
        # Güvenlik sınırı: paket uygulamanın veritabanı şifresini hiç görmez.
        self.write(".env", "DB_PASSWORD=masked\nVITALWATCH_KEY=lens\n")

        self.assertNotIn("DB_PASSWORD", env_module.env())

    def test_missing_file_does_not_crash(self):
        self.assertIsInstance(env_module.env(), dict)

    def test_non_utf8_file_is_skipped(self):
        # Windows'ta cp1254/latin-1 kaydedilmiş .env UnicodeDecodeError
        # fırlatıp uygulamanın açılışını düşürüyordu.
        Path(self.directory, ".env").write_bytes(b"VITALWATCH_URL=https://x\nNOT=\xe7\xfc\n")
        self.write(".env.local", "VITALWATCH_KEY=lens\n")

        values = env_module.env()

        self.assertNotIn("VITALWATCH_URL", values)
        self.assertEqual("lens", values["VITALWATCH_KEY"])


if __name__ == "__main__":
    unittest.main()
