"""Ajan: journald, Docker ve periyodik denetimler (alt süreç çalıştırılmaz)."""

import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_fixtures as F  # noqa: E402

from vitalwatch.agent import docker, journal, process, system  # noqa: E402


class JournalTest(unittest.TestCase):
    def test_unit_failure(self):
        event = journal.analyze(F.JOURNAL_UNIT_FAILED)

        self.assertEqual(
            {
                "kind": "service_failed",
                "msg": "php8.4-fpm.service: Failed with result 'exit-code'",
                "file": "journald",
                "command": "php8.4-fpm.service",
            },
            event,
        )

    def test_only_final_failure_and_system_manager(self):
        self.assertIsNone(journal.analyze(F.JOURNAL_MAIN_EXITED))
        self.assertIsNone(journal.analyze(F.JOURNAL_USER_UNIT_FAILED))

    def test_oom(self):
        event = journal.analyze(F.JOURNAL_OOM)

        self.assertEqual("oom", event["kind"])
        self.assertEqual("Out of memory: Killed process <n> (php-fpm8.4)", event["msg"])
        self.assertEqual("php-fpm8.4", event["command"])
        self.assertEqual("Out of memory: Killed process <n> (node)", journal.analyze(F.JOURNAL_CGROUP_OOM)["msg"])
        self.assertEqual("oom", journal.analyze(F.JOURNAL_OOMD)["kind"])

    def test_segfault_addresses_dropped(self):
        event = journal.analyze(F.JOURNAL_SEGFAULT)

        self.assertEqual("kernel", event["kind"])
        self.assertEqual("php-fpm8.4[<n>]: segfault in libc.so.6", event["msg"])

    def test_kernel_errors(self):
        self.assertIn("I/O error", journal.analyze(F.JOURNAL_IO_ERROR)["msg"])
        self.assertIn("EXT4-fs error", journal.analyze(F.JOURNAL_BYTES_MESSAGE)["msg"])

    def test_all_errors_mode(self):
        self.assertIsNone(journal.analyze(F.JOURNAL_APP_ERR))

        event = journal.analyze(F.JOURNAL_APP_ERR, all_errors=True)

        self.assertEqual("log_error", event["kind"])
        self.assertEqual("database connection lost to [ip], retrying", event["msg"])
        self.assertEqual("myapp.service", event["command"])

        # Kimlik doğrulama logları o modda da gelmez; ajan kendini raporlamaz.
        self.assertIsNone(journal.analyze(F.JOURNAL_SSHD, all_errors=True))
        own = dict(F.JOURNAL_APP_ERR, _SYSTEMD_UNIT="vitalwatch-agent.service", SYSLOG_IDENTIFIER="python3")
        self.assertIsNone(journal.analyze(own, all_errors=True))

    def test_watcher_cursor_and_unit_callback(self):
        emitted, noted = [], []
        watcher = journal.JournalWatcher(emitted.append, on_unit_failed=noted.append)

        self.assertEqual(["journalctl", "-f", "-o", "json", "--no-pager", "-q", "-n", "0"], watcher.command())

        for entry in (F.JOURNAL_MAIN_EXITED, F.JOURNAL_UNIT_FAILED):
            watcher.handle_line(json.dumps(entry))
        watcher.handle_line("not json")

        self.assertEqual("s=abc;i=1", watcher.cursor)
        self.assertEqual(1, len(emitted))
        self.assertEqual(["php8.4-fpm.service"], noted)
        self.assertEqual(["--after-cursor", "s=abc;i=1"], watcher.command()[-2:])

    def test_saved_cursor_used(self):
        self.assertIn("--after-cursor", journal.JournalWatcher(lambda _: None, cursor="s=1").command())


class DockerTest(unittest.TestCase):
    def setUp(self):
        self.watcher = docker.DockerWatcher(lambda _: None)

    def feed(self, *events):
        result = []

        for item in events:
            result.extend(self.watcher.analyze(item))

        return [event["msg"] for event in result]

    def test_nonzero_exit(self):
        self.assertEqual(["api: exited with code 137"], self.feed(F.docker_event("die", exit_code=137)))
        self.assertEqual([], self.feed(F.docker_event("die", exit_code=0, at=1_761_050_000)))

    def test_new_format_without_legacy_fields(self):
        self.assertEqual(["api: exited with code 1"], self.feed(F.docker_event("die", exit_code=1, legacy=False)))

    def test_restart_loop(self):
        start = 1_761_040_000
        messages = self.feed(*(F.docker_event("die", exit_code=1, at=start + i * 60) for i in range(4)))

        self.assertEqual(4, messages.count("api: exited with code 1"))
        self.assertEqual(["api: restart loop (3 in 10 min)"], [m for m in messages if "loop" in m])

        # Aynı 10 dakikada ikinci döngü olayı yok; sonra yeniden.
        self.assertEqual([], [m for m in self.feed(F.docker_event("die", exit_code=1, at=start + 300)) if "loop" in m])
        later = [F.docker_event("die", exit_code=1, at=start + 700 + i * 30) for i in range(3)]
        self.assertEqual(1, len([m for m in self.feed(*later) if "loop" in m]))

    def test_slow_restarts_are_not_a_loop(self):
        messages = self.feed(*(F.docker_event("die", exit_code=1, at=1_761_040_000 + i * 400) for i in range(5)))

        self.assertFalse([m for m in messages if "loop" in m])

    def test_loop_per_container(self):
        start = 1_761_040_000
        events = [F.docker_event("die", name=name, exit_code=0, at=start + i) for i in range(2) for name in ("a", "b")]

        self.assertFalse([m for m in self.feed(*events) if "loop" in m])

    def test_intentional_stop_ignored(self):
        start = 1_761_040_000
        messages = self.feed(
            F.docker_event("kill", at=start),
            F.docker_event("die", exit_code=143, at=start + 10),
            F.docker_event("stop", at=start + 20),
            F.docker_event("die", exit_code=137, at=start + 25),
        )

        self.assertEqual([], messages)

    def test_oom_and_unhealthy(self):
        self.assertEqual(["api: killed by OOM"], self.feed(F.docker_event("oom")))
        self.assertEqual(["api: unhealthy"], self.feed(F.docker_event("health_status: unhealthy")))
        self.assertEqual([], self.feed(F.docker_event("health_status: healthy")))
        self.assertEqual([], self.feed(F.docker_event("start")))
        self.assertEqual([], self.feed(dict(F.docker_event("die", exit_code=1), Type="network")))

    def test_handle_line(self):
        emitted = []
        watcher = docker.DockerWatcher(emitted.append)
        watcher.handle_line(json.dumps(F.docker_event("die", exit_code=2)))
        watcher.handle_line("garbage")

        self.assertEqual(
            [{"kind": "container_failed", "msg": "api: exited with code 2", "file": "docker", "command": "api"}], emitted
        )

    def test_container_logs(self):
        emitted = []
        watcher = docker.DockerLogWatcher("api", emitted.append)
        watcher.handle_line("2026-10-21T10:00:00Z ERROR db timeout talking to 10.0.0.5:5432 for /orders?id=7")
        watcher.handle_line("2026-10-21T10:00:01Z INFO all fine")

        self.assertEqual(["docker", "logs", "-f", "--tail", "0", "api"], watcher.command())
        self.assertEqual(1, len(emitted))
        self.assertEqual("ERROR db timeout talking to [ip] for /orders", emitted[0]["msg"])
        self.assertEqual("docker:api", emitted[0]["file"])


def stats(blocks=1000, bfree=100, bavail=50, files=1000, ffree=500):
    return SimpleNamespace(f_blocks=blocks, f_bfree=bfree, f_bavail=bavail, f_files=files, f_ffree=ffree)


MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/loop0 /snap/core/1 squashfs ro 0 0
overlay /var/lib/docker/overlay2/x/merged overlay rw 0 0
/dev/sdb1 /var/my\\040data xfs rw 0 0
/dev/sda1 /mnt/bind ext4 rw 0 0
/dev/sdc1 /srv btrfs rw 0 0
"""


class DiskTest(unittest.TestCase):
    def test_real_mounts(self):
        self.assertEqual(["/", "/var/my data", "/srv"], system.real_mounts(MOUNTS))

    def test_usage_df_formula(self):
        disk, inodes = system.usage(stats(blocks=1000, bfree=100, bavail=50, files=0, ffree=0))

        self.assertAlmostEqual(900 * 100.0 / 950, disk)
        self.assertIsNone(inodes)

    def test_threshold_and_hourly_dedupe(self):
        emitted = []
        clock = [0.0]
        values = {
            "/": stats(bfree=60, bavail=60),  # %94
            "/var/my data": stats(bfree=500, bavail=500, files=100, ffree=5),  # disk %50, inode %95
            "/srv": OSError("izin yok"),
        }

        def statvfs(path):
            value = values[path]
            if isinstance(value, Exception):
                raise value
            return value

        checker = system.DiskChecker(emitted.append, threshold=90, statvfs=statvfs, mounts_reader=lambda: MOUNTS, clock=lambda: clock[0])
        checker.check()

        self.assertEqual(["/: disk 94% full", "/var/my data: inodes 95% used"], [e["msg"] for e in emitted])
        self.assertTrue(all(e["kind"] == "disk_full" and e["file"] == "disk" for e in emitted))

        clock[0] += 3599
        checker.check()
        self.assertEqual(2, len(emitted))

        clock[0] += 1
        checker.check()
        self.assertEqual(4, len(emitted))

    def test_below_threshold(self):
        emitted = []
        checker = system.DiskChecker(
            emitted.append, threshold=90, statvfs=lambda _: stats(bfree=200, bavail=200), mounts_reader=lambda: MOUNTS
        )
        checker.check()

        self.assertEqual([], emitted)

    def test_dedupe_survives_restart(self):
        """Ajan yeniden başlayınca aynı saat içinde aynı disk olayı tekrarlanmaz."""
        full = lambda _: stats(bfree=60, bavail=60)  # %94
        root_only = lambda: "/dev/sda1 / ext4 rw 0 0\n"
        clock = [10_000.0]
        first = []

        before = system.DiskChecker(first.append, statvfs=full, mounts_reader=root_only, clock=lambda: clock[0])
        before.check()
        self.assertEqual(1, len(first))

        saved = json.loads(json.dumps(before.known()))  # durum dosyası tur atışı
        clock[0] += 600

        second = []
        after = system.DiskChecker(second.append, statvfs=full, mounts_reader=root_only, clock=lambda: clock[0], known=saved)
        after.check()
        self.assertEqual([], second)

        clock[0] += 3000
        after.check()
        self.assertEqual(1, len(second))

    def test_corrupt_saved_entries_are_ignored(self):
        checker = system.DiskChecker(lambda _: None, known={"bozuk": 1, "/|disk": "dün", "/|swap": 5.0, "/var|inodes": 7.0})

        self.assertEqual({("/var", "inodes"): 7.0}, checker._last)


class FailedUnitsTest(unittest.TestCase):
    def runner(self, *outputs):
        queue = list(outputs)

        def run(command, **_):
            return subprocess.CompletedProcess(command, 0, queue.pop(0), "")

        return run

    def test_dedupe_and_recovery(self):
        emitted = []
        checker = system.FailedUnitsChecker(
            emitted.append,
            runner=self.runner(
                "nginx.service loaded failed failed A high performance web server\n",
                "nginx.service loaded failed failed x\n",
                "",
                "● nginx.service loaded failed failed x\nvitalwatch-agent.service loaded failed failed y\n",
            ),
        )

        for _ in range(4):
            checker.check()

        self.assertEqual(["nginx.service: failed (systemctl --failed)"] * 2, [e["msg"] for e in emitted])
        self.assertEqual("nginx.service", emitted[0]["command"])
        self.assertEqual("systemd", emitted[0]["file"])

    def test_known_from_state_and_journal(self):
        emitted = []
        checker = system.FailedUnitsChecker(
            emitted.append, known=["redis.service"], runner=self.runner("redis.service loaded failed failed x\nphp8.4-fpm.service loaded failed failed y\n")
        )
        checker.note_failed("php8.4-fpm.service")
        checker.check()

        self.assertEqual([], emitted)

    def test_missing_systemctl(self):
        warnings = []

        def run(*_, **__):
            raise FileNotFoundError

        checker = system.FailedUnitsChecker(lambda _: None, runner=run, warn=warnings.append)
        checker.check()
        checker.check()

        self.assertFalse(checker.available)
        self.assertEqual(1, len(warnings))


class LineProcessTest(unittest.TestCase):
    def test_missing_binary_warns_once_and_stops(self):
        warnings = []
        thread = process.LineProcess(
            "journal", ["vitalwatch-no-such-binary-xyz"], lambda _: None, threading.Event(), warnings.append
        )
        thread.start()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertFalse(thread.available)
        self.assertEqual(1, len(warnings))
        self.assertIn("not found", warnings[0])

    def test_lines_delivered_and_exit_warned(self):
        lines, warnings = [], []
        stop = threading.Event()
        command = [sys.executable, "-c", "import sys; print('a'); print('b'); sys.stderr.write('permission denied\\n')"]

        def on_line(line):
            lines.append(line)

        def warn(message):
            warnings.append(message)
            stop.set()  # ilk çıkışta yeniden denemeden dur

        thread = process.LineProcess("test", command, on_line, stop, warn)
        thread.start()
        thread.join(timeout=10)

        self.assertEqual(["a", "b"], lines)
        self.assertEqual(1, len(warnings))
        self.assertIn("permission denied", warnings[0])

    def test_stop_terminates_child(self):
        stop = threading.Event()
        thread = process.LineProcess("test", [sys.executable, "-c", "import time; time.sleep(60)"], lambda _: None, stop, lambda _: None)
        thread.start()

        for _ in range(50):
            if thread._process is not None:
                break
            threading.Event().wait(0.05)

        stop.set()
        thread.stop()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
