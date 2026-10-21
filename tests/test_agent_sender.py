"""Ajan: hız sınırı, parçalama, yeniden deneme, canlılık — sahte hub'a karşı."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hub import FakeHub  # noqa: E402

from vitalwatch.agent import sender as sender_module  # noqa: E402
from vitalwatch.agent.sender import Sender, chunk, identity  # noqa: E402
from vitalwatch.client import MAX_BODY_BYTES, HubClient  # noqa: E402
from vitalwatch.reporter import SDK_VERSION  # noqa: E402

SECRET = "s" * 64


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


def event(index=0, msg=None, kind="web_server"):
    return {"kind": kind, "msg": msg or f"upstream timed out {index}", "file": "/var/log/nginx/error.log"}


class RateLimitTest(unittest.TestCase):
    def test_same_message_capped_per_minute(self):
        clock = Clock()
        sender = Sender(RecordingClient(), clock=clock, warn=lambda _: None)

        accepted = [sender.submit(event(msg="same")) for _ in range(30)]

        self.assertEqual(20, sum(accepted))
        self.assertEqual(10, sender.suppressed)

        # Aynı mesaj farklı log dosyasında ayrı sayılır.
        self.assertTrue(sender.submit(dict(event(msg="same"), file="/var/log/other.log")))

        clock.now += 61
        self.assertTrue(sender.submit(event(msg="same")))

    def test_total_cap(self):
        sender = Sender(RecordingClient(), clock=Clock(), warn=lambda _: None)

        accepted = sum(sender.submit(event(i)) for i in range(400))

        self.assertEqual(300, accepted)

    def test_queue_drops_oldest(self):
        clock = Clock()
        sender = Sender(RecordingClient(), clock=clock, warn=lambda _: None)

        for i in range(1100):
            if i % 300 == 0:
                clock.now += 61
            sender.submit(event(i))

        self.assertEqual(1000, sender.pending())
        self.assertEqual(100, sender.dropped)
        self.assertEqual("upstream timed out 100", sender._queue[0]["msg"])

    def test_disabled_sends_nothing(self):
        client = RecordingClient()
        sender = Sender(client, enabled=False, clock=Clock())

        self.assertFalse(sender.submit(event()))
        sender.flush(force=True)
        self.assertIsNone(sender.heartbeat())
        self.assertEqual([], client.bodies)


class ChunkTest(unittest.TestCase):
    def test_chunks_fit_body_and_event_limits(self):
        events = [event(i, msg=f"{i} " + "ğ" * 480) for i in range(40)]
        bodies = chunk(events, identity())

        self.assertEqual(40, sum(len(items) for _, items in bodies))

        for body, items in bodies:
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(raw), MAX_BODY_BYTES)
            self.assertLessEqual(len(items), sender_module.MAX_EVENTS_PER_REQUEST)
            self.assertEqual("agent", body["runtime"])

    def test_small_events_capped_at_eight(self):
        bodies = chunk([event(i) for i in range(25)], identity())

        self.assertEqual([8, 8, 8, 1], [len(items) for _, items in bodies])

    def test_oversized_single_event_truncated(self):
        huge = event(msg="a " * 5000)
        (body, items), = chunk([huge], identity())

        self.assertEqual(200, len(items[0]["msg"]))


class DeliveryTest(unittest.TestCase):
    def test_batch_accepted_by_hub_with_identity(self):
        with FakeHub(SECRET) as hub:
            client = HubClient(url=hub.url, key="vps", secret=SECRET)
            sender = Sender(client, env="production", clock=Clock(), warn=lambda _: None)

            for i in range(12):
                sender.submit(event(i))

            sender.flush()

        self.assertEqual(2, len(hub.requests))

        for record in hub.requests:
            self.assertTrue(record["accepted"], "hub imzayı kabul etmeli")
            self.assertEqual("/api/i/vps/server", record["path"])
            body = record["body"]
            self.assertEqual("agent", body["runtime"])
            self.assertEqual(SDK_VERSION, body["sdk_version"])
            self.assertEqual(sys.version.split()[0], body["runtime_version"])
            self.assertEqual("production", body["env"])

        self.assertEqual(12, sum(len(record["body"]["events"]) for record in hub.requests))
        self.assertEqual(12, sender.sent_events)

    def test_heartbeat_body(self):
        with FakeHub(SECRET) as hub:
            sender = Sender(HubClient(url=hub.url, key="vps", secret=SECRET), env="staging", clock=Clock(), warn=lambda _: None)

            self.assertEqual("ok", sender.heartbeat())

        body = hub.requests[0]["body"]
        self.assertTrue(hub.requests[0]["accepted"])
        self.assertEqual([], body["events"])
        self.assertEqual({"events", "env", "runtime", "runtime_version", "sdk_version"}, set(body))
        self.assertEqual("staging", body["env"])

    def test_heartbeat_schedule(self):
        clock = Clock()
        client = RecordingClient()
        sender = Sender(client, clock=clock, warn=lambda _: None)

        sender.tick()  # açılışta hemen
        self.assertEqual(1, len(client.bodies))

        clock.now += 299
        sender.tick()
        self.assertEqual(1, len(client.bodies))

        clock.now += 1
        sender.tick()
        self.assertEqual(2, len(client.bodies))

        # Olay gönderimi de canlılık sayılır: ardından boş istek gitmez.
        clock.now += 300
        sender.submit(event())
        sender.tick()
        self.assertEqual(3, len(client.bodies))
        self.assertEqual(1, len(client.bodies[-1]["events"]))

    def test_flush_interval_and_count(self):
        clock = Clock()
        client = RecordingClient()
        sender = Sender(client, clock=clock, warn=lambda _: None)
        sender._last_success = clock.now  # canlılığı devre dışı bırak
        sender._last_flush = clock.now

        sender.submit(event(1))
        sender.tick()
        self.assertEqual(0, len(client.bodies), "5 sn dolmadan gönderilmez")

        clock.now += 5
        sender.tick()
        self.assertEqual(1, len(client.bodies))

        for i in range(25):
            sender.submit(event(100 + i))
        sender.tick()
        self.assertEqual(0, sender.pending(), "25 olay birikince beklemeden")

    def test_network_failure_requeues_with_backoff(self):
        clock = Clock()
        failure = {"sent": False, "status": None, "error": "Connection refused"}
        client = RecordingClient([failure, failure])
        warnings = []
        sender = Sender(client, clock=clock, warn=warnings.append)

        for i in range(3):
            sender.submit(event(i))

        sender.flush()
        self.assertEqual(3, sender.pending())
        self.assertEqual(1, len(warnings))

        sender.flush()  # geri çekilme süresi dolmadı
        self.assertEqual(1, len(client.bodies))

        clock.now += 5
        sender.flush()  # ikinci başarısızlık: 10 sn
        self.assertEqual(2, len(client.bodies))
        self.assertEqual(clock.now + 10, sender._next_attempt)
        self.assertEqual(1, len(warnings), "kesinti bir kez yazılır")

        clock.now += 10
        sender.flush()
        self.assertEqual(0, sender.pending())
        self.assertEqual(3, len(client.bodies[-1]["events"]))
        self.assertIn("yeniden ulaşıldı", warnings[-1])

    def test_backoff_capped(self):
        clock = Clock()
        sender = Sender(RecordingClient(), clock=clock, warn=lambda _: None)

        for _ in range(20):
            sender._failed(clock.now, "x")

        self.assertEqual(clock.now + sender_module.BACKOFF_MAX, sender._next_attempt)

    def test_server_error_retried_client_error_dropped(self):
        clock = Clock()
        client = RecordingClient([{"sent": False, "status": 503, "error": None}, {"sent": False, "status": 301, "error": None}])
        sender = Sender(client, clock=clock, warn=lambda _: None)

        sender.submit(event(1))
        sender.flush()
        self.assertEqual(1, sender.pending())

        clock.now += 10
        sender.flush()
        self.assertEqual(0, sender.pending(), "3xx/4xx yeniden denenmez")

    def test_unreachable_hub_real_client(self):
        sender = Sender(HubClient(url="http://127.0.0.1:9", key="k", secret=SECRET, timeout=0.5), clock=Clock(), warn=lambda _: None)
        sender.submit(event())
        sender.flush()

        self.assertEqual(1, sender.pending())

    def test_thread_start_stop_flushes(self):
        with FakeHub(SECRET) as hub:
            sender = Sender(HubClient(url=hub.url, key="vps", secret=SECRET), warn=lambda _: None)
            sender._last_success = sender.clock()  # canlılık isteğini atla
            sender._last_flush = sender.clock()
            sender.start()
            sender.submit(event())
            sender.stop(timeout=2)

        self.assertEqual(1, sum(len(r["body"]["events"]) for r in hub.requests))


if __name__ == "__main__":
    unittest.main()
