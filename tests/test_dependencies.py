"""Bağımlılık listesi (/deps): ad normalizasyonu, kanonik hash, sınırlar,
zamanlama (sahte saat), kapatma bayrağı, fork davranışı ve imzanın sahte hub'da
kabulü. Ağa çıkılmaz — yalnızca yerel sahte hub."""

import hashlib
import io
import os
import sys
import threading
import unittest
import warnings
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hub import FakeHub  # noqa: E402

import vitalwatch  # noqa: E402
from vitalwatch import dependencies  # noqa: E402
from vitalwatch import env as env_module  # noqa: E402
from vitalwatch.reporter import SDK_VERSION, Reporter  # noqa: E402

SECRET = "s" * 64
HOUR = 60 * 60
DAY = 24 * HOUR


class _Metadata(dict):
    """importlib.metadata.PackageMetadata yerine: yalnızca .get kullanılıyor."""


class FakeDistribution:
    def __init__(self, name, version, broken=False):
        self._name = name
        self._version = version
        self._broken = broken

    @property
    def metadata(self):
        if self._broken:
            raise KeyError("Name")

        return _Metadata(Name=self._name, Version=self._version)


def reporter_for(hub, **options):
    settings = {"url": hub.url, "key": "project", "secret": SECRET, "timeout": 2.0}
    settings.update(options)

    return Reporter(**settings)


class NormalizeTest(unittest.TestCase):
    def test_pep503(self):
        cases = {
            "Flask": "flask",
            "zope.interface": "zope-interface",
            "typing_extensions": "typing-extensions",
            "Foo__Bar--baz..Qux": "foo-bar-baz-qux",
            "  Django  ": "django",
            "a-_.b": "a-b",
        }

        for raw, expected in cases.items():
            self.assertEqual(expected, dependencies.normalize_name(raw), raw)


class CollectTest(unittest.TestCase):
    def test_normalized_sorted_deduped_and_excluded(self):
        packages = dependencies.collect([
            FakeDistribution("Werkzeug", "3.0.1"),
            FakeDistribution("Flask", "3.0.3"),
            FakeDistribution("flask", "2.0.0"),  # sys.path'te sonra: ilk gelen kazanır
            FakeDistribution("typing_extensions", " 4.12.2 "),
            FakeDistribution("pip", "24.0"),
            FakeDistribution("setuptools", "69.0.0"),
            FakeDistribution("wheel", "0.43.0"),
            FakeDistribution("pkg_resources", "0.0.0"),
            FakeDistribution("VitalWatch", SDK_VERSION),
            FakeDistribution("broken", "1.0", broken=True),
            FakeDistribution("", "1.0"),
            FakeDistribution("noversion", None),
        ])

        self.assertEqual(
            [
                {"name": "flask", "version": "3.0.3"},
                {"name": "typing-extensions", "version": "4.12.2"},
                {"name": "werkzeug", "version": "3.0.1"},
            ],
            packages,
        )

    def test_cap(self):
        many = [FakeDistribution(f"pkg{index:05d}", "1.0") for index in range(dependencies.MAX_PACKAGES + 50)]

        packages = dependencies.collect(reversed(many))

        self.assertEqual(dependencies.MAX_PACKAGES, len(packages))
        self.assertEqual("pkg00000", packages[0]["name"])
        self.assertEqual(f"pkg{dependencies.MAX_PACKAGES - 1:05d}", packages[-1]["name"])

    def test_never_raises(self):
        def exploding():
            yield FakeDistribution("a", "1")
            raise RuntimeError("bozuk sys.path")

        self.assertEqual([], dependencies.collect(exploding()))

    def test_real_interpreter(self):
        packages = dependencies.collect()

        names = [item["name"] for item in packages]
        self.assertEqual(sorted(names), names)
        self.assertEqual(len(set(names)), len(names))
        self.assertFalse({"pip", "setuptools", "wheel", "vitalwatch"} & set(names))


class HashTest(unittest.TestCase):
    def test_canonical_hash(self):
        packages = [{"name": "flask", "version": "3.0.3"}, {"name": "django", "version": "5.0"}]

        expected = hashlib.sha256(b"django@5.0\nflask@3.0.3\n").hexdigest()

        self.assertEqual(expected, dependencies.canonical_hash(packages))
        self.assertEqual(expected, dependencies.canonical_hash(list(reversed(packages))))
        self.assertEqual(hashlib.sha256(b"").hexdigest(), dependencies.canonical_hash([]))

    def test_hash_changes_with_version(self):
        first = dependencies.canonical_hash([{"name": "flask", "version": "3.0.3"}])
        second = dependencies.canonical_hash([{"name": "flask", "version": "3.0.4"}])

        self.assertNotEqual(first, second)

    def test_manifest(self):
        packages = [{"name": "flask", "version": "3.0.3"}]

        self.assertEqual(
            {
                "v": 1,
                "ecosystem": "PyPI",
                "manifest": "installed-distributions",
                "hash": dependencies.canonical_hash(packages),
                "packages": packages,
            },
            dependencies.manifest(packages),
        )


class FlagTest(unittest.TestCase):
    def test_enabled_in(self):
        self.assertTrue(dependencies.enabled_in({}))

        for value in ("true", "1", "yes", "on", "evet", "anything"):
            self.assertTrue(dependencies.enabled_in({"VITALWATCH_DEPENDENCIES": value}), value)

        for value in ("false", "FALSE", "0", "no", "off", " Off ", "disabled", "hayir", "kapali", "kapalı"):
            self.assertFalse(dependencies.enabled_in({"VITALWATCH_DEPENDENCIES": value}), value)

    def test_from_environment(self):
        with mock.patch.object(vitalwatch, "read_env", return_value={"VITALWATCH_DEPENDENCIES": "false"}):
            self.assertFalse(vitalwatch.from_environment(detect_release=False)["dependencies"])

        with mock.patch.object(vitalwatch, "read_env", return_value={}):
            self.assertTrue(vitalwatch.from_environment(detect_release=False)["dependencies"])


class SendTest(unittest.TestCase):
    PACKAGES = [{"name": "flask", "version": "3.0.3"}, {"name": "werkzeug", "version": "3.0.1"}]

    def test_signed_body_accepted_by_hub(self):
        with FakeHub(SECRET) as hub:
            result = reporter_for(hub, env="staging", release="abc123").send_dependencies(packages=self.PACKAGES)

        self.assertTrue(result["sent"])
        self.assertEqual(204, result["status"])
        self.assertEqual(1, len(hub.requests))

        request = hub.requests[0]
        self.assertEqual("/api/i/project/deps", request["path"])
        self.assertTrue(request["accepted"])

        body = request["body"]
        self.assertEqual("staging", body["env"])
        self.assertEqual("server", body["source"])
        self.assertEqual("abc123", body["release"])
        self.assertEqual("python", body["runtime"])
        self.assertEqual(sys.version.split()[0], body["runtime_version"])
        self.assertEqual(SDK_VERSION, body["sdk_version"])
        self.assertNotIn("events", body)
        self.assertEqual(dependencies.manifest(self.PACKAGES), body["dependencies"])

    def test_large_body_not_trimmed_to_8kb(self):
        packages = [{"name": f"package-{index:04d}", "version": "1.0.0"} for index in range(1000)]

        with FakeHub(SECRET) as hub:
            result = reporter_for(hub).send_dependencies(packages=packages)

        self.assertTrue(result["sent"])
        self.assertGreater(len(hub.requests[0]["raw"]), 8192)
        self.assertTrue(hub.requests[0]["accepted"])
        self.assertEqual(1000, len(hub.requests[0]["body"]["dependencies"]["packages"]))

    def test_size_guard_sends_nothing_and_warns_once(self):
        packages = [{"name": "x" * 200 + str(index), "version": "1.0.0"} for index in range(3000)]
        err = io.StringIO()

        with mock.patch.object(dependencies, "_warned_too_large", False), \
                FakeHub(SECRET) as hub, redirect_stderr(err):
            reporter = reporter_for(hub)
            first = reporter.send_dependencies(packages=packages)
            second = reporter.send_dependencies(packages=packages)

        self.assertEqual([], hub.requests)
        self.assertEqual("body-too-large", first["error"])
        self.assertEqual("body-too-large", second["error"])
        self.assertGreater(first["size"], dependencies.MAX_BODY_BYTES)
        self.assertEqual(1, err.getvalue().count("dependency list"))
        self.assertIn("not sent", err.getvalue())

    def test_disabled_flag_and_enabled_false_send_nothing(self):
        with FakeHub(SECRET) as hub:
            disabled = reporter_for(hub, dependencies=False).send_dependencies(packages=self.PACKAGES)
            off = reporter_for(hub, enabled=False).send_dependencies(packages=self.PACKAGES)

            self.assertFalse(reporter_for(hub, dependencies=False).wants_dependencies())
            self.assertFalse(reporter_for(hub, enabled=False).wants_dependencies())

        self.assertEqual("disabled", disabled["error"])
        self.assertEqual("missing-config", off["error"])
        self.assertEqual([], hub.requests)

    def test_never_raises(self):
        reporter = Reporter(url="http://127.0.0.1:9", key="k", secret=SECRET, timeout=0.2)

        with mock.patch.object(dependencies, "manifest", side_effect=RuntimeError("x")):
            self.assertFalse(reporter.send_dependencies(packages=[])["sent"])


class FakeReporter:
    def __init__(self, clock, wants=True, sent=True):
        self.clock = clock
        self.wants = wants
        self.sent = sent
        self.dependency_sends = []
        self.heartbeats = []

    def wants_dependencies(self):
        return self.wants

    def send_dependencies(self, packages=None):
        self.dependency_sends.append((self.clock.now, dependencies.canonical_hash(packages)))

        if self.sent:
            return {"sent": True, "status": 204, "error": None}

        return {"sent": False, "status": None, "error": "timed out"}

    def heartbeat(self, block=True):
        self.heartbeats.append(self.clock.now)

        return {"sent": True, "status": 204, "error": None}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeStop:
    """``stop.wait`` saati ilerletir; ``until`` anına gelince döngü durur."""

    def __init__(self, clock, until):
        self.clock = clock
        self.until = until

    def wait(self, seconds):
        self.clock.now += seconds

        return self.clock.now >= self.until


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.packages = [{"name": "flask", "version": "3.0.3"}]

    def collect(self):
        return list(self.packages)

    def test_startup_delay_then_daily(self):
        schedule = dependencies.Schedule(self.clock.now)
        fake = FakeReporter(self.clock)

        self.assertFalse(schedule.due(self.clock.now + 59))
        self.assertTrue(schedule.due(self.clock.now + 60))

        start = self.clock.now
        sent_at = []

        for hours in range(0, 50):
            now = start + 60 + hours * HOUR
            self.clock.now = now

            if schedule.due(now) and schedule.run(now, fake, self.collect):
                sent_at.append(now - start)

        self.assertEqual([60, 60 + DAY, 60 + 2 * DAY], sent_at)

    def test_hash_change_sends_at_next_check(self):
        schedule = dependencies.Schedule(0)
        fake = FakeReporter(self.clock)

        self.assertIsNotNone(schedule.run(60, fake, self.collect))
        self.assertIsNone(schedule.run(60 + HOUR, fake, self.collect))

        self.packages = [{"name": "flask", "version": "3.0.4"}]

        self.assertIsNotNone(schedule.run(60 + 2 * HOUR, fake, self.collect))
        self.assertEqual(60 + 3 * HOUR, schedule.next_check)
        self.assertEqual(2, len(fake.dependency_sends))
        self.assertNotEqual(fake.dependency_sends[0][1], fake.dependency_sends[1][1])

    def test_network_failure_retries_next_check(self):
        schedule = dependencies.Schedule(0)
        fake = FakeReporter(self.clock, sent=False)

        schedule.run(60, fake, self.collect)
        fake.sent = True
        schedule.run(60 + HOUR, fake, self.collect)
        schedule.run(60 + 2 * HOUR, fake, self.collect)

        self.assertEqual(2, len(fake.dependency_sends))

    def test_disabled_never_collects_or_sends(self):
        schedule = dependencies.Schedule(0)
        fake = FakeReporter(self.clock, wants=False)
        collect = mock.Mock(side_effect=AssertionError("toplanmamalı"))

        self.assertIsNone(schedule.run(60, fake, collect))
        self.assertEqual([], fake.dependency_sends)
        self.assertEqual(60 + HOUR, schedule.next_check)

    def test_run_never_raises(self):
        schedule = dependencies.Schedule(0)
        broken = mock.Mock()
        broken.wants_dependencies.side_effect = RuntimeError("x")

        self.assertIsNone(schedule.run(60, broken))
        self.assertEqual(60 + HOUR, schedule.next_check)


class TimerLoopTest(unittest.TestCase):
    """Canlılık thread'inin gerçek döngüsü, sahte saatle 49 saat."""

    def run_loop(self, wants=True, hours=49, packages_by_time=None):
        clock = FakeClock()
        start = clock.now
        fake = FakeReporter(clock, wants=wants)
        schedule = dependencies.Schedule(start)

        def collect():
            elapsed = clock.now - start
            version = "2.0" if packages_by_time and elapsed >= packages_by_time else "1.0"

            return [{"name": "flask", "version": version}]

        with mock.patch.object(vitalwatch, "reporter", return_value=fake), \
                mock.patch.object(dependencies, "collect", side_effect=collect):
            vitalwatch._timer_loop(FakeStop(clock, start + hours * HOUR), 8 * HOUR, schedule, clock)

        return start, fake

    def test_heartbeat_and_dependencies_schedule(self):
        start, fake = self.run_loop()

        self.assertEqual([60, 60 + DAY, 60 + 2 * DAY], [at - start for at, _ in fake.dependency_sends])
        self.assertEqual([8 * HOUR * n for n in range(1, 7)], [at - start for at in fake.heartbeats])

    def test_hash_change_mid_day(self):
        start, fake = self.run_loop(hours=12, packages_by_time=5 * HOUR)

        self.assertEqual([60, 60 + 5 * HOUR], [at - start for at, _ in fake.dependency_sends])

    def test_disabled_sends_only_heartbeats(self):
        _, fake = self.run_loop(wants=False)

        self.assertEqual([], fake.dependency_sends)
        self.assertEqual(6, len(fake.heartbeats))


class _Isolated(unittest.TestCase):
    def setUp(self):
        saved_environ = {k: v for k, v in os.environ.items() if k.startswith("VITALWATCH_")}

        for key in saved_environ:
            del os.environ[key]

        saved = (vitalwatch._instance, vitalwatch._hooks_installed)
        env_module.forget()
        vitalwatch.stop_heartbeat()

        def restore():
            vitalwatch.stop_heartbeat()
            vitalwatch._instance, vitalwatch._hooks_installed = saved
            os.environ.update(saved_environ)
            env_module.forget()

        self.addCleanup(restore)


class HeartbeatThreadTest(_Isolated):
    def test_start_does_not_send_dependencies_immediately(self):
        with FakeHub(SECRET) as hub:
            current = vitalwatch.init(url=hub.url, key="project", secret=SECRET, timeout=2.0, release="r")
            vitalwatch.start_heartbeat()
            current.flush(5)

        self.assertTrue(hub.requests)
        self.assertFalse(any(r["path"].endswith("/deps") for r in hub.requests))

    @unittest.skipUnless(hasattr(os, "fork"), "fork yok")
    def test_forked_child_does_not_start_its_own_sender(self):
        with FakeHub(SECRET) as hub:
            current = vitalwatch.init(url=hub.url, key="project", secret=SECRET, timeout=2.0, release="r")
            parent_thread = vitalwatch.start_heartbeat()
            current.flush(5)

            self.assertTrue(parent_thread.is_alive())

            with warnings.catch_warnings():
                # 3.12+: thread'li süreçte fork DeprecationWarning verir.
                warnings.simplefilter("ignore", DeprecationWarning)
                pid = os.fork()

            if pid == 0:  # pragma: no cover — çocuk süreç
                code = 1

                try:
                    again = vitalwatch.start_heartbeat()
                    alive = [t for t in threading.enumerate() if t.name == "heartbeat" and t.is_alive()]

                    if again is parent_thread and not again.is_alive() and not alive:
                        code = 0
                finally:
                    os._exit(code)

            _, status = os.waitpid(pid, 0)

            self.assertEqual(0, os.WEXITSTATUS(status))
            self.assertTrue(parent_thread.is_alive())


class AgentTest(unittest.TestCase):
    def test_agent_never_sends_dependencies(self):
        root = Path(__file__).resolve().parents[1] / "src" / "vitalwatch" / "agent"

        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")

            self.assertNotIn("send_dependencies", text, path.name)
            self.assertNotIn("start_heartbeat", text, path.name)
            self.assertNotIn('"deps"', text, path.name)


if __name__ == "__main__":
    unittest.main()
