"""``vitalwatch-status``: sürüm etiketi satırı ve PyPI güncelleme denetimi. Ağa çıkılmaz."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch import cli  # noqa: E402

CONFIG = {"VITALWATCH_URL": "https://hub.sample", "VITALWATCH_KEY": "k", "VITALWATCH_SECRET": "a" * 64, "VITALWATCH_ENV": "local"}


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self, *_):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def pypi(version):
    """PyPI JSON API yanıtının ilgili kısmı."""
    return _Response({"info": {"name": "vitalwatch", "version": version}, "releases": {}})


class StatusTest(unittest.TestCase):
    def run_cli(self, config=None, urlopen=None, release=(None, None), version="0.2.0"):
        config = dict(CONFIG if config is None else config)
        out, err = io.StringIO(), io.StringIO()
        urlopen = urlopen or mock.Mock(side_effect=AssertionError("ağa çıkılmamalı"))

        environ = {k: v for k, v in os.environ.items() if k != "VITALWATCH_STATUS_OFFLINE"}

        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(cli, "read_env", return_value=config), \
                mock.patch.object(cli, "detect_release", return_value=release), \
                mock.patch.object(cli, "__version__", version), \
                mock.patch.object(cli, "init"), \
                mock.patch.object(cli.urllib.request, "urlopen", urlopen), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main([])

        return code, out.getvalue(), err.getvalue(), urlopen

    def test_new_version_warning_does_not_change_exit_code(self):
        code, out, _, urlopen = self.run_cli(
            urlopen=mock.Mock(return_value=pypi("0.10.1"))
        )

        self.assertEqual(0, code)
        self.assertIn("Latest version", out)
        self.assertIn("0.10.1", out)
        self.assertIn("Update available: pip install -U vitalwatch", out)
        self.assertNotIn("git+", out)

        request = urlopen.call_args[0][0]
        self.assertEqual("https://pypi.org/pypi/vitalwatch/json", request.full_url)
        self.assertTrue(request.get_header("User-agent"))
        self.assertEqual(2.0, urlopen.call_args[1]["timeout"])

    def test_no_warning_when_up_to_date(self):
        for installed in ("0.2.0", "0.3.0"):
            code, out, _, _ = self.run_cli(urlopen=mock.Mock(return_value=pypi("0.2.0")), version=installed)

            self.assertEqual(0, code)
            self.assertIn("0.2.0", out)
            self.assertNotIn("Update available", out)

    def test_prerelease_not_counted(self):
        for version in ("0.3.0-beta", "0.3.0rc1"):
            _, out, _, _ = self.run_cli(urlopen=mock.Mock(return_value=pypi(version)))

            self.assertNotIn("Update available", out)
            self.assertNotIn("0.3.0", out)

    def test_network_error_check_failed(self):
        import urllib.error

        for failure in (
            urllib.error.URLError("ağ yok"),
            urllib.error.HTTPError(cli.PYPI_URL, 404, "Not Found", {}, None),
            TimeoutError("zaman aşımı"),
        ):
            code, out, _, _ = self.run_cli(urlopen=mock.Mock(side_effect=failure))

            self.assertEqual(0, code)
            self.assertIn("could not check", out)
            self.assertNotIn("Update available", out)

    def test_broken_response_or_missing_tag_check_failed(self):
        for response in (_Response({"message": "Not Found"}), _Response([]), _Response(["1.0.0"]), _Response({"info": {}}), _Response({"info": {"version": None}})):
            _, out, _, _ = self.run_cli(urlopen=mock.Mock(return_value=response))

            self.assertIn("could not check", out)

    def test_cevrimdisi_degerleri(self):
        for flag in ("1", "true", "TRUE", "yes", "on", "On", "evet", "EVET"):
            config = dict(CONFIG, VITALWATCH_STATUS_OFFLINE=flag)
            code, out, _, _ = self.run_cli(config=config)

            self.assertEqual(0, code, flag)
            self.assertNotIn("Latest version", out, flag)

        for flag in ("0", "false", "hayir", ""):
            config = dict(CONFIG, VITALWATCH_STATUS_OFFLINE=flag)
            _, out, _, urlopen = self.run_cli(config=config, urlopen=mock.Mock(return_value=pypi("0.2.0")))

            self.assertIn("Latest version", out, flag)
            self.assertEqual(1, urlopen.call_count, flag)

    def test_offline_skips_check(self):
        for source in ("environ", "env-dosyasi"):
            config = dict(CONFIG)

            if source == "env-dosyasi":
                config["VITALWATCH_STATUS_OFFLINE"] = "1"
                code, out, _, _ = self.run_cli(config=config)
            else:
                with mock.patch.dict(os.environ, {"VITALWATCH_STATUS_OFFLINE": "1"}):
                    out = io.StringIO()
                    with mock.patch.object(cli, "read_env", return_value=config), \
                            mock.patch.object(cli, "init"), \
                            mock.patch.object(cli.urllib.request, "urlopen",
                                              side_effect=AssertionError("ağa çıkılmamalı")), \
                            redirect_stdout(out), redirect_stderr(io.StringIO()):
                        code = cli.main([])
                    out = out.getvalue()

            self.assertEqual(0, code, source)
            self.assertNotIn("Latest version", out, source)

    def test_version_tag_line(self):
        environ = {"VITALWATCH_STATUS_OFFLINE": "1"}
        cases = (
            (("abc123def456", ".git"), "abc123def456 (source: .git)"),
            (("v2", "VITALWATCH_RELEASE"), "v2 (source: VITALWATCH_RELEASE)"),
            (("0123456789ab", "HEROKU_SLUG_COMMIT"), "0123456789ab (source: HEROKU_SLUG_COMMIT)"),
            ((None, None), "not set (source: none)"),
        )

        for detected, expected in cases:
            out = io.StringIO()

            with mock.patch.dict(os.environ, environ), \
                    mock.patch.object(cli, "read_env", return_value=CONFIG), \
                    mock.patch.object(cli, "detect_release", return_value=detected), \
                    mock.patch.object(cli, "init"), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                cli.main([])

            line = next(l for l in out.getvalue().splitlines() if "Release tag" in l)
            self.assertTrue(line.rstrip().endswith(expected), line)

    def test_version_comparison_is_numeric(self):
        self.assertGreater(cli._stable("v0.10.0"), cli._stable("0.9.9"))
        self.assertIsNone(cli._stable("v1.0.0-rc1"))
        self.assertIsNone(cli._stable("1.0"))
        self.assertIsNone(cli._stable(None))
        self.assertEqual((1, 2, 3), cli._stable(" V1.2.3 "))


if __name__ == "__main__":
    unittest.main()
