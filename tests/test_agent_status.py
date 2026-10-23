"""Ajan: sunucu durum özeti — ayrıştırma, kırpma, zamanlama, komut, sahte hub."""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hub import FakeHub  # noqa: E402

from vitalwatch.agent import cli, config, runner, status  # noqa: E402
from vitalwatch.agent.sender import Sender, identity  # noqa: E402
from vitalwatch.client import HubClient  # noqa: E402

SECRET = "t" * 64

MEMINFO = """\
MemTotal:        6082016 kB
MemFree:          512000 kB
MemAvailable:    3775488 kB
Buffers:          100000 kB
SwapCached:        10000 kB
SwapTotal:       1887436 kB
SwapFree:        1031372 kB
"""

UPTIME = "15897600.42 61234567.10\n"

MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sdb1 /var/lib/docker ext4 rw 0 0
/dev/loop0 /snap/core/1 squashfs ro 0 0
"""

DOCKER_INSPECT = json.dumps([
    {
        "Id": "aaa111", "Name": "/teklif-app-1", "RestartCount": 0,
        "State": {"Status": "running", "StartedAt": "2026-10-23T12:40:00.123456789Z",
                  "Health": {"Status": "healthy", "Log": []}},
    },
    {
        "Id": "bbb222", "Name": "/worker", "RestartCount": 7,
        "State": {"Status": "running", "StartedAt": "2026-10-22T08:00:00Z"},
    },
    {
        "Id": "ccc333", "Name": "/never-started", "RestartCount": 0,
        "State": {"Status": "created", "StartedAt": "0001-01-01T00:00:00Z"},
    },
])

APT_SIMULATE = """\
NOTE: This is only a simulation!
Reading package lists...
Inst libssl3 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-security [amd64])
Inst openssl [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-security [amd64])
Inst curl [8.5.0-2ubuntu10.4] (8.5.0-2ubuntu10.5 Ubuntu:24.04/noble-updates [amd64])
Conf libssl3 (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-security [amd64])
"""


def stats(blocks=1000, bfree=400, bavail=350, files=100, ffree=88, frsize=4096 * 1000):
    return SimpleNamespace(f_blocks=blocks, f_bfree=bfree, f_bavail=bavail, f_files=files, f_ffree=ffree, f_frsize=frsize)


class FakeRunner:
    """subprocess.run yerine: komutun ilk iki öğesine göre yanıt."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs.get("timeout")))
        key = command[0] if command[0] != "docker" else " ".join(command[:2])
        response = self.responses.get(key)

        if response is None:
            raise FileNotFoundError(command[0])
        if isinstance(response, Exception):
            raise response

        code, stdout, stderr = response

        return subprocess.CompletedProcess(command, code, stdout, stderr)


def make_collector(runner_responses=None, files=None, exists=(), which=("docker", "systemctl", "apt-get"), **kwargs):
    files = files if files is not None else {"/proc/uptime": UPTIME, "/proc/meminfo": MEMINFO, "/proc/mounts": MOUNTS}
    responses = {
        "systemctl": (0, "● logrotate.service loaded failed failed Rotate\nvitalwatch-agent.service loaded failed failed X\n", ""),
        "docker ps": (0, "aaa111\nbbb222\nccc333\n", ""),
        "docker inspect": (0, DOCKER_INSPECT, ""),
        status.APT_CHECK: (0, "", "10;3"),
        "apt-get": (0, APT_SIMULATE, ""),
    }
    responses.update(runner_responses or {})

    def reader(path):
        if path not in files:
            raise OSError(path)
        return files[path]

    defaults = dict(
        runner=FakeRunner(responses),
        reader=reader,
        statvfs=lambda path: stats(),
        which=lambda name: f"/usr/bin/{name}" if name in which else None,
        exists=lambda path: path in exists,
        wall_clock=lambda: 1761234567.9,
        hostname=lambda: "kind-elion.example.com",
        loadavg=lambda: (0.0, 0.1234, 0.179),
        cpu_count=lambda: 4,
    )
    defaults.update(kwargs)

    return status.StatusCollector(**defaults)


class ParseTest(unittest.TestCase):
    def test_uptime(self):
        self.assertEqual(15897600, status.parse_uptime(UPTIME))

    def test_meminfo(self):
        self.assertEqual(
            {"total_mb": 5939, "available_mb": 3687, "swap_total_mb": 1843, "swap_used_mb": 836},
            status.parse_meminfo(MEMINFO),
        )

    def test_meminfo_missing_field(self):
        with self.assertRaises(ValueError):
            status.parse_meminfo("MemTotal: 100 kB\n")

    def test_disk_entry(self):
        entry = status.disk_entry("/", stats())

        self.assertEqual({"mount": "/", "used_pct": 63.2, "inodes_pct": 12.0, "free_gb": 1.4}, entry)
        self.assertIsNone(status.disk_entry("/", stats(files=0))["inodes_pct"])
        self.assertIsNone(status.disk_entry("/x", stats(blocks=0)))

    def test_docker_inspect(self):
        containers = status.parse_docker_inspect(DOCKER_INSPECT)
        by_name = {container["name"]: container for container in containers}

        self.assertEqual(
            {"name": "teklif-app-1", "state": "running", "health": "healthy", "restarts": 0,
             "started_at": "2026-10-23T12:40:00Z"},
            by_name["teklif-app-1"],
        )
        self.assertIsNone(by_name["worker"]["health"], "Health yoksa null")
        self.assertEqual(7, by_name["worker"]["restarts"])
        self.assertIsNone(by_name["never-started"]["started_at"], "sıfır zaman null")
        self.assertEqual("never-started", containers[0]["name"], "sorunlu konteyner öne")

    def test_docker_inspect_limits(self):
        many = [{"Name": "/" + "x" * 100 + str(i), "State": {"Status": "running"}} for i in range(40)]
        containers = status.parse_docker_inspect(json.dumps(many))

        self.assertEqual(status.MAX_CONTAINERS, len(containers))
        self.assertTrue(all(len(container["name"]) == 64 for container in containers))

    def test_apt_check(self):
        self.assertEqual((10, 3), status.parse_apt_check("10;3"))
        self.assertEqual((0, 0), status.parse_apt_check("W: something\n0;0\n"))
        self.assertIsNone(status.parse_apt_check("E: broken"))

    def test_apt_simulate(self):
        self.assertEqual((3, 2), status.parse_apt_simulate(APT_SIMULATE))
        self.assertEqual((0, 0), status.parse_apt_simulate("Reading package lists...\n"))


class CollectorTest(unittest.TestCase):
    def test_full_snapshot_matches_contract(self):
        snapshot = make_collector(exists=(status.APT_CHECK, status.REBOOT_REQUIRED)).collect()

        self.assertEqual(
            ["v", "collected_at", "hostname", "uptime_s", "load", "cpus", "memory", "disks",
             "failed_units", "containers", "updates"],
            list(snapshot),
        )
        self.assertEqual(1, snapshot["v"])
        self.assertEqual(1761234567, snapshot["collected_at"])
        self.assertEqual("kind-elion", snapshot["hostname"])
        self.assertEqual(15897600, snapshot["uptime_s"])
        self.assertEqual([0.0, 0.12, 0.18], snapshot["load"])
        self.assertEqual(4, snapshot["cpus"])
        self.assertEqual(["/", "/var/lib/docker"], [disk["mount"] for disk in snapshot["disks"]])
        self.assertEqual(["logrotate.service"], snapshot["failed_units"], "ajanın kendi birimi hariç")
        self.assertEqual(3, len(snapshot["containers"]))
        self.assertEqual({"pending": 10, "security": 3, "reboot_required": True}, snapshot["updates"])
        json.dumps(snapshot)

    def test_no_ip_addresses(self):
        text = json.dumps(make_collector().collect())

        self.assertNotRegex(text, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_apt_get_fallback_when_apt_check_missing(self):
        collector = make_collector()  # apt-check dosyası yok
        snapshot = collector.collect()

        self.assertEqual({"pending": 3, "security": 2, "reboot_required": False}, snapshot["updates"])
        self.assertEqual("apt-get", collector.update_method())
        apt_calls = [call for call in collector.runner.calls if call[0][0] == "apt-get"]
        self.assertEqual(status.APT_SIMULATE, apt_calls[0][0])
        self.assertEqual(status.APT_TIMEOUT, apt_calls[0][1])

    def test_apt_get_fallback_when_apt_check_output_unusable(self):
        collector = make_collector({status.APT_CHECK: (1, "", "Traceback ...")}, exists=(status.APT_CHECK,))

        self.assertEqual(3, collector.collect()["updates"]["pending"])

    def test_non_apt_system(self):
        collector = make_collector(which=("docker", "systemctl"))

        self.assertIsNone(collector.collect()["updates"])
        self.assertIsNone(collector.update_method())

    def test_updates_cached_for_an_hour(self):
        now = [0.0]
        collector = make_collector(exists=(status.APT_CHECK,), clock=lambda: now[0])

        def apt_calls():
            return sum(1 for command, _ in collector.runner.calls if command[0] == status.APT_CHECK)

        collector.collect()
        now[0] += 3599
        collector.collect()
        self.assertEqual(1, apt_calls())

        now[0] += 1
        collector.collect()
        self.assertEqual(2, apt_calls())

    def test_docker_respects_config_and_availability(self):
        self.assertIsNone(make_collector(docker_mode="off").collect()["containers"])
        self.assertIsNone(make_collector(which=("systemctl",)).collect()["containers"])
        denied = make_collector({"docker ps": (1, "", "permission denied while trying to connect")})
        self.assertIsNone(denied.collect()["containers"])
        empty = make_collector({"docker ps": (0, "\n", "")})
        self.assertEqual([], empty.collect()["containers"])

    def test_systemctl_missing_or_none_failed(self):
        self.assertIsNone(make_collector({"systemctl": FileNotFoundError("systemctl")}).collect()["failed_units"])
        self.assertEqual([], make_collector({"systemctl": (0, "", "")}).collect()["failed_units"])

    def test_timeouts_passed(self):
        collector = make_collector(exists=(status.APT_CHECK,))
        collector.collect()

        for command, timeout in collector.runner.calls:
            expected = status.APT_TIMEOUT if command[0] in (status.APT_CHECK, "apt-get") else status.COMMAND_TIMEOUT
            self.assertEqual(expected, timeout, command)

    def test_collector_exception_yields_null(self):
        def broken():
            raise RuntimeError("boom")

        collector = make_collector(
            {"docker ps": subprocess.TimeoutExpired("docker", 20), "systemctl": OSError("x")},
            files={"/proc/mounts": MOUNTS},
            loadavg=broken,
            hostname=broken,
        )
        snapshot = collector.collect()

        for field in ("hostname", "uptime_s", "load", "memory", "failed_units", "containers"):
            self.assertIsNone(snapshot[field], field)

        self.assertEqual(4, snapshot["cpus"])
        self.assertEqual(2, len(snapshot["disks"]))
        self.assertEqual(1, snapshot["v"])


class TrimTest(unittest.TestCase):
    def snapshot(self, containers=200, units=100, disks=10):
        base = make_collector().collect()
        base["containers"] = [
            {"name": f"container-{i:03d}-" + "n" * 40, "state": "running", "health": "healthy",
             "restarts": 0, "started_at": "2026-10-23T12:40:00Z"}
            for i in range(containers)
        ]
        base["failed_units"] = [f"unit-{i:03d}-" + "u" * 50 + ".service" for i in range(units)]
        base["disks"] = [{"mount": f"/mnt/{i}", "used_pct": 1.0, "inodes_pct": 1.0, "free_gb": 1.0} for i in range(disks)]

        return base

    def size(self, body):
        return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def test_small_body_untouched(self):
        snapshot = make_collector().collect()
        body = status.status_body(identity(), snapshot)

        self.assertEqual(snapshot, body["server_status"])
        self.assertNotIn("truncated", body["server_status"])
        self.assertEqual([], body["events"])

    def test_containers_trimmed_first(self):
        snapshot = self.snapshot(containers=200, units=5)
        body = status.status_body(identity(), snapshot)
        trimmed = body["server_status"]

        self.assertLessEqual(self.size(body), status.STATUS_BODY_LIMIT)
        self.assertTrue(trimmed["truncated"])
        self.assertLess(len(trimmed["containers"]), 200)
        self.assertEqual(snapshot["containers"][: len(trimmed["containers"])], trimmed["containers"], "sondan kırpılır")
        self.assertEqual(5, len(trimmed["failed_units"]), "konteyner yetince birimlere dokunulmaz")
        self.assertEqual(200, len(snapshot["containers"]), "girdi değişmez")

    def test_then_units_then_disks(self):
        snapshot = self.snapshot(containers=10, units=400, disks=10)
        body = status.status_body(identity(), snapshot, limit=1500)
        trimmed = body["server_status"]

        self.assertLessEqual(self.size(body), 1500)
        self.assertEqual([], trimmed["containers"])
        self.assertTrue(trimmed["truncated"])
        self.assertLess(len(trimmed["failed_units"]), 400)

        body = status.status_body(identity(), snapshot, limit=1000)
        self.assertEqual([], body["server_status"]["failed_units"])
        self.assertLess(len(body["server_status"]["disks"]), 10)
        self.assertLessEqual(self.size(body), 1000)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class RecordingClient:
    def __init__(self, results=None):
        self.bodies = []
        self.results = list(results or [])

    def send(self, body):
        self.bodies.append(json.loads(json.dumps(body)))

        return self.results.pop(0) if self.results else {"sent": True, "status": 204, "error": None}


class StopAfter:
    """``threading.Event`` yerine: ``wait`` sürelerini kaydeder, n turdan sonra durur."""

    def __init__(self, rounds, on_wait=None):
        self.rounds = rounds
        self.waits = []
        self.on_wait = on_wait

    def is_set(self):
        return len(self.waits) >= self.rounds

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait()
        return self.is_set()


class ScheduleTest(unittest.TestCase):
    def make_agent(self, client):
        loaded = config.load("/nonexistent/agent.ini", environ={
            "VITALWATCH_URL": "https://hub.example.com", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
        })
        agent = runner.Agent(loaded, client=client, store=mock.Mock(), warn_func=lambda _: None)
        agent.sender.clock = Clock()

        return agent

    def test_first_at_start_then_every_300_seconds_while_events_flow(self):
        client = RecordingClient()
        agent = self.make_agent(client)
        clock = agent.sender.clock
        collected = []

        def collect():
            collected.append(clock.now)
            return {"v": 1, "collected_at": int(clock.now), "hostname": "h"}

        agent.status_collector = SimpleNamespace(collect=collect)

        def between_rounds():
            # 300 sn boyunca olaylar akıyor: her 5 sn'de bir olay gönderimi.
            for _ in range(60):
                clock.now += 5
                agent.sender.submit({"kind": "log_error", "msg": f"err {clock.now}", "file": "/x"})
                agent.sender.tick()

        agent.stop_event = StopAfter(3, on_wait=between_rounds)

        # İlk özet açılışta, beklemeden önce.
        agent.sender.tick()
        agent._status_loop()

        self.assertEqual([status.STATUS_INTERVAL] * 3, agent.stop_event.waits)
        self.assertEqual([1000.0, 1300.0, 1600.0], collected)

        status_bodies = [body for body in client.bodies if "server_status" in body]
        event_bodies = [body for body in client.bodies if body["events"]]

        # Her turun özeti bir sonraki tick'te gider.
        self.assertEqual([1000, 1300, 1600], [body["server_status"]["collected_at"] for body in status_bodies])
        self.assertGreater(len(event_bodies), 100, "olaylar akarken")
        self.assertTrue(all(body["events"] == [] for body in status_bodies))
        self.assertFalse(
            any(body["events"] == [] and "server_status" not in body for body in client.bodies[1:]),
            "açılıştan sonra ayrı canlılık isteği gerekmedi",
        )

    def test_status_counts_as_heartbeat(self):
        client = RecordingClient()
        sender = Sender(client, clock=Clock(), warn=lambda _: None)
        sender.set_status({"v": 1})
        sender.tick()

        self.assertEqual(1, len(client.bodies))
        self.assertIn("server_status", client.bodies[0])
        self.assertFalse(sender.heartbeat_due())

    def test_only_latest_kept_while_hub_down(self):
        clock = Clock()
        failure = {"sent": False, "status": None, "error": "Connection refused"}
        client = RecordingClient([failure])
        sender = Sender(client, clock=clock, warn=lambda _: None)
        sender._last_success = clock.now

        sender.set_status({"v": 1, "collected_at": 1})
        sender.tick()
        self.assertEqual(1, len(client.bodies))

        sender.set_status({"v": 1, "collected_at": 2})
        sender.tick()
        self.assertEqual(1, len(client.bodies), "geri çekilme süresinde gönderilmez")

        clock.now += 5
        sender.tick()
        self.assertEqual(2, client.bodies[-1]["server_status"]["collected_at"])

        clock.now += 5
        sender.tick()
        self.assertEqual(2, len(client.bodies), "gönderilen özet tekrar gitmez")

    def test_rejected_status_discarded(self):
        warnings = []
        client = RecordingClient([{"sent": False, "status": 403, "error": None}])
        sender = Sender(client, clock=Clock(), warn=warnings.append)
        sender._last_success = sender.clock()
        sender.set_status({"v": 1})
        sender.tick()
        sender.tick()

        self.assertEqual(1, len(client.bodies))
        self.assertIn("server status rejected", warnings[0])

    def test_disabled_sends_nothing(self):
        client = RecordingClient()
        sender = Sender(client, enabled=False, clock=Clock(), warn=lambda _: None)
        sender.set_status({"v": 1})
        sender.send_status()

        self.assertEqual([], client.bodies)


class HubTest(unittest.TestCase):
    def test_signed_body_carries_server_status_and_is_accepted(self):
        snapshot = make_collector(exists=(status.APT_CHECK,)).collect()

        with FakeHub(SECRET) as hub:
            sender = Sender(HubClient(url=hub.url, key="vps", secret=SECRET), env="production", clock=Clock(), warn=lambda _: None)
            sender.set_status(snapshot)
            self.assertEqual("ok", sender.send_status())

        record = hub.requests[0]
        self.assertTrue(record["accepted"], "hub imzayı kabul etmeli")
        self.assertEqual("/api/i/vps/server", record["path"])
        body = record["body"]
        self.assertEqual({"env", "runtime", "runtime_version", "sdk_version", "events", "server_status"}, set(body))
        self.assertEqual("agent", body["runtime"])
        self.assertEqual([], body["events"])
        self.assertEqual(snapshot, body["server_status"])
        self.assertLessEqual(len(record["raw"]), status.STATUS_BODY_LIMIT)

    def test_trimmed_body_is_accepted(self):
        snapshot = TrimTest().snapshot(containers=300, units=200)

        with FakeHub(SECRET) as hub:
            sender = Sender(HubClient(url=hub.url, key="vps", secret=SECRET), clock=Clock(), warn=lambda _: None)
            sender.set_status(snapshot)
            sender.send_status()

        record = hub.requests[0]
        self.assertTrue(record["accepted"])
        self.assertTrue(record["body"]["server_status"]["truncated"])
        self.assertLessEqual(len(record["raw"]), status.STATUS_BODY_LIMIT)


class CommandTest(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        environ = {
            "VITALWATCH_URL": "https://hub.example.com", "VITALWATCH_KEY": "vps", "VITALWATCH_SECRET": SECRET,
            config.CONFIG_ENV: "/nonexistent/agent.ini",
        }

        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(cli, "collector_for", lambda _config: make_collector(exists=(status.APT_CHECK,))), \
                mock.patch.object(cli, "source_report", return_value=[("nginx", runner.FOUND, "1 file")]), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)

        return code, out.getvalue(), err.getvalue()

    def test_status_prints_json_and_sends_nothing(self):
        with FakeHub(SECRET) as hub:
            with mock.patch.dict(os.environ, {"VITALWATCH_URL": hub.url}):
                code, out, _ = self.run_cli(["status"])

        self.assertEqual(0, code)
        data = json.loads(out)
        self.assertEqual(1, data["v"])
        self.assertEqual("kind-elion", data["hostname"])
        self.assertEqual({"pending": 10, "security": 3, "reboot_required": False}, data["updates"])
        self.assertIn('\n  "hostname"', out, "girintili JSON")
        self.assertEqual([], hub.requests)

    def test_help_mentions_status(self):
        out = io.StringIO()

        with redirect_stdout(out):
            cli.main([])

        self.assertIn("status", out.getvalue())

    def test_check_shows_status_line(self):
        code, out, _ = self.run_cli(["check"])

        self.assertEqual(0, code)
        self.assertIn("Status snapshot    every 5 min (updates: apt-check)", out)


if __name__ == "__main__":
    unittest.main()
