"""Ajan: yapılandırma önceliği, yok sayma, komutlar ve uçtan uca çalışma."""

import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_fixtures as F  # noqa: E402
from fake_hub import FakeHub  # noqa: E402

from vitalwatch.agent import cli, config, runner  # noqa: E402
from vitalwatch.agent.state import StateStore  # noqa: E402

SECRET = "k" * 64

INI = """\
[hub]
url = https://hub.example.com
key = vps-key
secret = {secret}
env = prod

[sources]
nginx = auto
redis = off
docker = on            ; satır sonu yorumu
journal_errors = on
docker_logs = api, worker

[disk]
threshold = 85

[ignore]
hosts = status.example.com other.example.com
patterns =
    healthcheck
    ^GET /ping

[log:plesk-panel]
path = /var/log/plesk/panel.log
pattern = \\b(ERR|CRIT)\\b 100%
kind = log_error

[log:bad]
path = /var/log/x.log
pattern = ([unclosed

[log:weird-kind]
path = /var/log/y.log
pattern = x
kind = nonsense
"""


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "agent.ini")

        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(INI.format(secret=SECRET))

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_file_values(self):
        loaded = config.load(self.path, environ={})

        self.assertTrue(loaded.file_found)
        self.assertEqual("https://hub.example.com", loaded.url)
        self.assertEqual("vps-key", loaded.key)
        self.assertEqual(SECRET, loaded.secret)
        self.assertEqual("production", loaded.env, "takma ad çevrilir")
        self.assertEqual("off", loaded.source_mode("redis"))
        self.assertEqual("on", loaded.source_mode("docker"))
        self.assertEqual("auto", loaded.source_mode("mysql"))
        self.assertTrue(loaded.journal_errors)
        self.assertEqual(["api", "worker"], loaded.docker_logs)
        self.assertEqual(85, loaded.disk_threshold)
        self.assertEqual(["status.example.com", "other.example.com"], loaded.ignore_hosts)
        self.assertEqual(["healthcheck", "^GET /ping"], [p.pattern for p in loaded.ignore_patterns])
        self.assertEqual(["plesk-panel", "weird-kind"], [c["name"] for c in loaded.custom_logs])
        self.assertEqual(r"\b(ERR|CRIT)\b 100%", loaded.custom_logs[0]["pattern"].pattern, "interpolasyon kapalı")
        self.assertEqual("log_error", loaded.custom_logs[1]["kind"])
        self.assertEqual(2, len(loaded.errors), loaded.errors)

    def test_env_overrides_file(self):
        environ = {
            "VITALWATCH_URL": "https://other.example.com",
            "VITALWATCH_KEY": "env-key",
            "VITALWATCH_SECRET": "e" * 64,
            "VITALWATCH_ENV": "staging",
            "VITALWATCH_ENABLED": "false",
            "VITALWATCH_KEY_UNRELATED": "x",
            "OTHER": "y",
        }
        loaded = config.load(self.path, environ=environ)

        self.assertEqual("https://other.example.com", loaded.url)
        self.assertEqual("env-key", loaded.key)
        self.assertEqual("e" * 64, loaded.secret)
        self.assertEqual("staging", loaded.env)
        self.assertFalse(loaded.enabled)

    def test_empty_env_does_not_override(self):
        loaded = config.load(self.path, environ={"VITALWATCH_KEY": ""})

        self.assertEqual("vps-key", loaded.key)

    def test_path_precedence(self):
        self.assertEqual("/a.ini", config.resolve_path("/a.ini", {config.CONFIG_ENV: "/b.ini"}))
        self.assertEqual("/b.ini", config.resolve_path(None, {config.CONFIG_ENV: "/b.ini"}))
        self.assertEqual(config.DEFAULT_PATH, config.resolve_path(None, {}))

        loaded = config.load(environ={config.CONFIG_ENV: self.path})
        self.assertEqual(self.path, loaded.path)
        self.assertEqual("vps-key", loaded.key)

    def test_missing_file_env_only(self):
        loaded = config.load(os.path.join(self.directory, "none.ini"), environ={"VITALWATCH_URL": "https://h.example.com"})

        self.assertFalse(loaded.file_found)
        self.assertEqual([], loaded.errors)
        self.assertEqual("h.example.com", loaded.hub_host)

    def test_invalid_values_reported(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[sources]\nnginx = maybe\nfoo = on\n[disk]\nthreshold = 20\n[ignore]\npatterns = (\n")

        errors = config.load(self.path, environ={}).errors

        self.assertEqual(4, len(errors), errors)

    def test_hub_host_always_ignored(self):
        rules = config.load(self.path, environ={}).ignore_rules()

        self.assertIn("hub.example.com", rules.hosts)
        self.assertTrue(rules.matches_line(F.NGINX_TIMEOUT.replace("example.com", "hub.example.com")))
        self.assertFalse(rules.matches_line(F.NGINX_TIMEOUT))
        self.assertFalse(rules.matches_line(F.NGINX_TIMEOUT.replace("example.com", "hub.example.com.tr")))
        self.assertFalse(rules.matches_line(F.NGINX_TIMEOUT.replace("example.com", "myhub.example.com")))
        self.assertTrue(rules.matches_line("GET /x healthcheck failed"))
        self.assertTrue(rules.matches_event({"msg": "[hub.example.com] x", "file": "/var/log/nginx/error.log"}))
        self.assertTrue(rules.matches_event({"msg": "x", "file": "/var/www/vhosts/system/hub.example.com/logs/error_log"}))
        self.assertFalse(rules.matches_event({"msg": "[example.com] x", "file": "/x"}))


class AgentPipelineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def make_config(self, url="https://hub.example.com", **sources):
        loaded = config.load(os.path.join(self.directory, "none.ini"), environ={
            "VITALWATCH_URL": url, "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
        })

        for name in config.SOURCE_NAMES:
            loaded.sources[name] = "off"
        loaded.sources.update(sources)

        return loaded

    def test_hub_own_host_lines_dropped(self):
        agent = runner.Agent(self.make_config(), store=StateStore(self.directory), client=mock.Mock(), warn_func=lambda _: None)
        submitted = []
        agent.sender.submit = submitted.append

        from vitalwatch.agent.parsers import parse_nginx, parse_plesk_proxy

        agent.handle_line(F.NGINX_TIMEOUT.replace("example.com", "hub.example.com"), parse_nginx, "/var/log/nginx/error.log")
        agent.handle_line(F.NGINX_NO_SOCKET, parse_plesk_proxy, "/var/www/vhosts/system/hub.example.com/logs/proxy_error_log")
        agent.handle_line(F.NGINX_TIMEOUT, parse_nginx, "/var/log/nginx/error.log")
        agent.handle_line(F.NGINX_404_NOISE, parse_nginx, "/var/log/nginx/error.log")

        self.assertEqual(1, len(submitted))
        self.assertTrue(submitted[0]["msg"].startswith("[example.com] "))

    def test_file_sources_respect_modes(self):
        loaded = self.make_config(nginx="auto", plesk="on")
        names = {name for _, _, name in runner.file_sources(loaded)}

        self.assertEqual({"nginx", "plesk"}, names)

    def test_end_to_end_run_with_custom_log(self):
        log = os.path.join(self.directory, "app.log")

        with open(log, "w", encoding="utf-8") as handle:
            handle.write("[2026-10-21 10:00:00] ERR old line must not be sent\n")

        with FakeHub(SECRET) as hub:
            loaded = self.make_config(url=hub.url)
            import re

            loaded.custom_logs = [{"name": "app", "path": log, "pattern": re.compile(r"\bERR\b"), "kind": "log_error"}]
            store = StateStore(os.path.join(self.directory, "state"))
            agent = runner.Agent(loaded, store=store, warn_func=lambda _: None)
            # Gerçek alt süreç (docker, apt) çalışmasın.
            agent.status_collector = mock.Mock(collect=lambda: {"v": 1, "collected_at": 1, "hostname": "test"})

            with mock.patch.object(runner, "POLL_INTERVAL", 0.05):
                thread = threading.Thread(target=agent.run, kwargs={"install_signals": False})
                thread.start()

                for _ in range(100):
                    if agent.tailer is not None:
                        break
                    time.sleep(0.02)

                with open(log, "a", encoding="utf-8") as handle:
                    handle.write("[2026-10-21 10:00:01] ERR payment for ali@example.com from 203.0.113.7 failed /pay?card=1\n")
                    handle.write("[2026-10-21 10:00:02] INFO fine\n")

                for _ in range(200):
                    if any(r["body"] and r["body"].get("events") for r in hub.requests):
                        break
                    time.sleep(0.02)

                agent.request_stop()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertTrue(all(r["accepted"] for r in hub.requests))
        events = [event for r in hub.requests for event in r["body"]["events"]]

        self.assertEqual(
            [{"kind": "log_error", "msg": "ERR payment for [email] from [ip] failed /pay", "file": log}], events
        )
        self.assertTrue(any(r["body"]["events"] == [] for r in hub.requests), "açılış canlılık isteği")
        self.assertTrue(
            any((r["body"].get("server_status") or {}).get("hostname") == "test" for r in hub.requests),
            "açılışta durum özeti",
        )

        saved = store.load()
        self.assertEqual(os.path.getsize(log), saved["files"][log]["offset"], "kapanışta durum kaydedildi")


class CliTest(unittest.TestCase):
    def run_cli(self, argv, environ=None, rows=None):
        out, err = io.StringIO(), io.StringIO()
        rows = rows if rows is not None else [("nginx", runner.FOUND, "1 file"), ("docker", runner.NOT_FOUND, "docker not installed")]
        environ = environ if environ is not None else {
            "VITALWATCH_URL": "https://hub.example.com", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
            config.CONFIG_ENV: "/nonexistent/agent.ini",
        }

        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(cli, "source_report", return_value=rows), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)

        return code, out.getvalue(), err.getvalue()

    def test_check_ok(self):
        code, out, _ = self.run_cli(["check"])

        self.assertEqual(0, code)
        self.assertIn("✓ nginx", out)
        self.assertIn("not found", out)
        self.assertIn("hub.example.com", out)
        self.assertIn("Configuration OK", out)

    def test_check_short_secret(self):
        code, _, err = self.run_cli(["check"], environ={
            "VITALWATCH_URL": "https://hub.example.com", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": "x" * 63,
            config.CONFIG_ENV: "/nonexistent/agent.ini",
        })

        self.assertEqual(1, code)
        self.assertIn("must be 64 characters, got 63", err)

    def test_check_missing_config(self):
        code, _, err = self.run_cli(["check"], environ={config.CONFIG_ENV: "/nonexistent/agent.ini"})

        self.assertEqual(1, code)
        self.assertIn("url, key, secret not set", err)

    def test_source_forced_on_but_missing_is_a_problem(self):
        code, _, err = self.run_cli(["check"], environ={
            "VITALWATCH_URL": "https://hub.example.com", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
            config.CONFIG_ENV: "/nonexistent/agent.ini",
        }, rows=[("docker", runner.NOT_FOUND, "")])

        self.assertEqual(0, code, "auto kaynak bulunamazsa sorun değil")

        with mock.patch.object(config.AgentConfig, "source_mode", lambda self, name: "on"):
            code, _, err = self.run_cli(["check"], rows=[("docker", runner.NOT_FOUND, "")])

        self.assertEqual(1, code)
        self.assertIn("docker source is 'on'", err)

    def test_check_test_event_accepted_by_hub(self):
        with FakeHub(SECRET) as hub:
            code, out, _ = self.run_cli(["check", "--test"], environ={
                "VITALWATCH_URL": hub.url, "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
                config.CONFIG_ENV: "/nonexistent/agent.ini",
            })

        # http:// uyarısı teşhisi düşürür; sahte hub http. O yüzden doğrudan gövdeye bakılmaz.
        self.assertEqual(1, code)
        self.assertEqual([], hub.requests, "yapılandırma sorunluyken gönderilmez")

        with FakeHub(SECRET) as hub, mock.patch.object(cli, "hub_problems", return_value=[]):
            code, out, _ = self.run_cli(["check", "--test"], environ={
                "VITALWATCH_URL": hub.url, "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
                config.CONFIG_ENV: "/nonexistent/agent.ini",
            })

        self.assertEqual(0, code)
        self.assertIn("Test event sent (HTTP 204)", out)
        record = hub.requests[0]
        self.assertTrue(record["accepted"])
        self.assertEqual("agent", record["body"]["runtime"])
        self.assertEqual("log_error", record["body"]["events"][0]["kind"])
        self.assertIn("Test event", record["body"]["events"][0]["msg"])

    def test_check_test_unreachable_reports_failure(self):
        with mock.patch.object(cli, "hub_problems", return_value=[]):
            code, _, err = self.run_cli(["check", "--test"], environ={
                "VITALWATCH_URL": "http://127.0.0.1:9", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
                config.CONFIG_ENV: "/nonexistent/agent.ini",
            })

        self.assertEqual(1, code)
        self.assertIn("NOT SENT", err)

    def test_systemd_unit(self):
        code, out, _ = self.run_cli(["systemd"])

        self.assertEqual(0, code)
        self.assertIn(f"ExecStart={sys.executable} -m vitalwatch.agent run --config /nonexistent/agent.ini", out)

        unit = cli.systemd_unit(python="/opt/vitalwatch/bin/python")

        for line in (
            "User=root",
            "ExecStart=/opt/vitalwatch/bin/python -m vitalwatch.agent run\n",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/vitalwatch-agent",
            "NoNewPrivileges=yes",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "Restart=always",
        ):
            self.assertIn(line, unit)

        self.assertIn("ProtectHome=read-only", cli.systemd_unit(python="/home/u/venv/bin/python"))
        self.assertIn("PrivateTmp=no", cli.systemd_unit(python="/tmp/venv/bin/python"))

    def test_no_command_prints_help(self):
        code, out, _ = self.run_cli([])

        self.assertEqual(2, code)
        self.assertIn("vitalwatch-agent", out)

    def test_run_http_is_only_a_warning(self):
        with mock.patch.object(cli, "Agent") as agent_class:
            code, _, err = self.run_cli(["run"], environ={
                "VITALWATCH_URL": "http://127.0.0.1:8080", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
                config.CONFIG_ENV: "/nonexistent/agent.ini",
            })

        self.assertEqual(0, code)
        self.assertIn("http://", err)
        agent_class.return_value.run.assert_called_once_with()

    def test_run_refuses_without_config(self):
        code, _, err = self.run_cli(["run"], environ={config.CONFIG_ENV: "/nonexistent/agent.ini"})

        self.assertEqual(1, code)
        self.assertIn("not set", err)


class SourceReportTest(unittest.TestCase):
    def test_statuses(self):
        directory = tempfile.mkdtemp()

        try:
            readable = os.path.join(directory, "app.log")
            Path(readable).write_text("x\n", encoding="utf-8")
            loaded = config.load(os.path.join(directory, "none.ini"), environ={})

            for name in config.SOURCE_NAMES:
                loaded.sources[name] = "off"
            import re

            loaded.custom_logs = [
                {"name": "app", "path": readable, "pattern": re.compile("x"), "kind": "log_error"},
                {"name": "gone", "path": os.path.join(directory, "none*.log"), "pattern": re.compile("x"), "kind": "log_error"},
            ]

            rows = {name: status for name, status, _ in runner.source_report(loaded)}
        finally:
            shutil.rmtree(directory, ignore_errors=True)

        self.assertEqual(runner.DISABLED, rows["nginx"])
        self.assertEqual(runner.DISABLED, rows["journal"])
        self.assertEqual(runner.DISABLED, rows["disk"])
        self.assertEqual(runner.FOUND, rows["log:app"])
        self.assertEqual(runner.NOT_FOUND, rows["log:gone"])


if __name__ == "__main__":
    unittest.main()
