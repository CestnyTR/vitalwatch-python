import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch.reporter import Reporter, normalize_env  # noqa: E402


class EnvironmentTest(unittest.TestCase):
    """Hub yalnızca production/staging/local kabul ediyor. Tanınmayan değer
    eskiden sessizce production sayılıyordu: test verisi canlıya karışıyordu.
    Tablo Node ve Laravel paketleriyle aynı."""

    def test_aliases_are_converted(self):
        for input, expected in (
            (None, "production"), ("", "production"), ("prod", "production"), ("Live", "production"),
            ("stage", "staging"), ("UAT", "staging"), ("preprod", "staging"),
            ("development", "local"), ("dev", "local"), ("testing", "local"), ("test", "local"),
            ("production", "production"), ("staging", "staging"), ("local", "local"),
        ):
            self.assertEqual(expected, normalize_env(input), input)

    def test_unknown_env_not_treated_as_production(self):
        self.assertEqual("qa-eu", normalize_env("qa-eu"))
        self.assertEqual("qa-eu", Reporter(env="qa-eu").env)

    def test_status_fails_on_unknown_env(self):
        from vitalwatch import cli

        env = {"VITALWATCH_URL": "https://hub.sample", "VITALWATCH_KEY": "k", "VITALWATCH_SECRET": "a" * 64, "VITALWATCH_ENV": "qa-eu",
                 # Güncelleme denetimi ağa çıkmasın.
                 "VITALWATCH_STATUS_OFFLINE": "1"}
        output, error = io.StringIO(), io.StringIO()

        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "read_env", return_value=env), \
                redirect_stdout(output), redirect_stderr(error):
            code = cli.main([])

        self.assertNotEqual(0, code)
        self.assertIn("is not accepted by the hub", error.getvalue())


if __name__ == "__main__":
    unittest.main()
