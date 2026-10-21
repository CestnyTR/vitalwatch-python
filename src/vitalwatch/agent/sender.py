"""Akış kontrolü ve gönderim.

- Aynı normalize mesaj (``kind`` + ``file`` + ``msg``) dakikada en fazla 20,
  toplam dakikada en fazla 300 olay; fazlası sayılır ve atlanır. Bir log
  fırtınası günlük kotayı tek hatayla bitirmesin.
- Kuyruk en fazla 1000 olay; taşarsa **en eski** atılır.
- Gönderim 5 sn'de bir ya da 25 olay birikince. Gövde hem 8 KB'ye hem hub'ın
  istek başına kabul ettiği 8 olaya (``IngestController::MAX_EVENTS``) göre
  parçalanır — fazlası hub'da sessizce kırpılırdı.
- Ağ hatası, 429 ve 5xx'te olaylar kuyruğa geri konur ve üstel geri çekilmeyle
  yeniden denenir. SDK'lardan farklı olarak yeniden deneme var: ajan ayrı bir
  süreç, bekletebileceği bir kullanıcı isteği yok.
- Son başarılı istekten bu yana 5 dk geçtiyse boş bir canlılık isteği
  (``{"events": [], …}``) gider; hub'ın kurulum sağlığı ajanın susmasını
  bununla görür.
"""

import collections
import json
import sys
import threading
import time

from ..client import MAX_BODY_BYTES
from ..reporter import SDK_VERSION

QUEUE_SIZE = 1000
PER_MESSAGE_LIMIT = 20
TOTAL_LIMIT = 300
WINDOW_SECONDS = 60.0
FLUSH_INTERVAL = 5.0
FLUSH_COUNT = 25
HEARTBEAT_INTERVAL = 300.0

#: Hub'ın tek istekte işlediği en fazla olay (vitalwatch-hub IngestController::MAX_EVENTS).
MAX_EVENTS_PER_REQUEST = 8

BACKOFF_START = 5.0
BACKOFF_MAX = 300.0


def identity(env="production"):
    """Her gövdenin üst seviyesindeki kimlik. Hub ``runtime: agent``'ı ``ServerSdk::Agent`` olarak damgalar."""
    return {
        "env": env,
        "runtime": "agent",
        "runtime_version": sys.version.split()[0],
        "sdk_version": SDK_VERSION,
    }


def _size(body):
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def chunk(events, head):
    """Olayları 8 KB ve 8 olay sınırına sığan gövdelere böler.

    Tek başına sığmayan olayın mesajı 200 karaktere kısaltılır; yine
    sığmazsa atılır (msg ≤ 500 olduğundan pratikte olmaz).

    :return: ``[(gövde, [olaylar]), …]``
    """
    bodies = []
    current = []

    def body_of(items):
        return dict(head, events=items)

    for event in events:
        if _size(body_of([event])) > MAX_BODY_BYTES:
            event = dict(event, msg=str(event.get("msg", ""))[:200])

            if _size(body_of([event])) > MAX_BODY_BYTES:
                continue

        candidate = current + [event]

        if len(candidate) > MAX_EVENTS_PER_REQUEST or _size(body_of(candidate)) > MAX_BODY_BYTES:
            bodies.append((body_of(current), current))
            current = [event]
        else:
            current = candidate

    if current:
        bodies.append((body_of(current), current))

    return bodies


class Sender:
    def __init__(self, client, env="production", enabled=True, clock=time.monotonic, warn=None):
        self.client = client
        self.head = identity(env)
        self.enabled = enabled
        self.clock = clock
        self._warn = warn or (lambda message: print(f"vitalwatch-agent: {message}", file=sys.stderr, flush=True))

        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

        self._window_start = None
        self._message_counts = collections.Counter()
        self._window_total = 0
        self.suppressed = 0
        self.dropped = 0

        self._failures = 0
        self._next_attempt = 0.0
        self._last_flush = None
        self._last_success = None
        self._outage_reported = False
        self.sent_events = 0

    # ------------------------------------------------------------------ #
    # Kabul
    # ------------------------------------------------------------------ #

    def _roll_window(self, now):
        if self._window_start is None or now - self._window_start >= WINDOW_SECONDS:
            if self.suppressed:
                self._warn(f"son dakikada {self.suppressed} olay hız sınırı nedeniyle atlandı.")

            self._window_start = now
            self._message_counts.clear()
            self._window_total = 0
            self.suppressed = 0

    def submit(self, event):
        """Hız sınırından geçerse kuyruğa koyar. Asla bloke etmez, hata fırlatmaz."""
        if not self.enabled or not event:
            return False

        with self._lock:
            now = self.clock()
            self._roll_window(now)
            key = (event.get("kind"), event.get("file"), event.get("msg"))

            if self._message_counts[key] >= PER_MESSAGE_LIMIT or self._window_total >= TOTAL_LIMIT:
                self.suppressed += 1
                return False

            self._message_counts[key] += 1
            self._window_total += 1
            self._queue.append(event)

            while len(self._queue) > QUEUE_SIZE:
                self._queue.popleft()
                self.dropped += 1

            ready = len(self._queue) >= FLUSH_COUNT

        if ready:
            self._wake.set()

        return True

    def pending(self):
        with self._lock:
            return len(self._queue)

    # ------------------------------------------------------------------ #
    # Gönderim
    # ------------------------------------------------------------------ #

    def _failed(self, now, reason):
        self._failures += 1
        delay = min(BACKOFF_START * (2 ** (self._failures - 1)), BACKOFF_MAX)
        self._next_attempt = now + delay

        if not self._outage_reported:
            self._outage_reported = True
            self._warn(f"hub'a gönderilemedi ({reason}); olaylar bekletiliyor, yeniden denenecek.")

    def _succeeded(self, now):
        if self._outage_reported:
            self._warn("hub'a yeniden ulaşıldı.")

        self._failures = 0
        self._next_attempt = 0.0
        self._outage_reported = False
        self._last_success = now

    def _post(self, body):
        """``"ok"``, ``"retry"`` ya da ``"drop"``."""
        try:
            result = self.client.send(body)
        except Exception as error:  # noqa: BLE001 — HubClient fırlatmaz ama yine de
            result = {"sent": False, "status": None, "error": str(error)}

        status = result.get("status")

        if result.get("sent"):
            return "ok", None

        if status is None:
            if result.get("error") in ("missing-config", "body-too-large"):
                return "drop", result.get("error")
            return "retry", result.get("error") or "ağ hatası"

        if status == 429 or status >= 500:
            return "retry", f"HTTP {status}"

        # 3xx (yönlendirme takip edilmez — muhtemelen http→https) ya da 4xx:
        # yeniden denemek bir şey değiştirmez.
        return "drop", f"HTTP {status}"

    def flush(self, force=False):
        """Kuyruktakileri gönderir. Geri çekilme süresindeyse (``force`` hariç) bekler."""
        if not self.enabled:
            return

        now = self.clock()

        if not force and now < self._next_attempt:
            return

        with self._lock:
            events = list(self._queue)
            self._queue.clear()

        self._last_flush = now

        if not events:
            return

        bodies = chunk(events, self.head)

        for index, (body, items) in enumerate(bodies):
            outcome, reason = self._post(body)

            if outcome == "ok":
                self.sent_events += len(items)
                self._succeeded(self.clock())
                continue

            if outcome == "drop":
                self._warn(f"{len(items)} olay hub tarafından reddedildi ({reason}); atıldı.")
                continue

            # Gönderilemeyenler kuyruğun başına; taşan en eskiler atılır.
            remaining = [event for _, chunk_items in bodies[index:] for event in chunk_items]

            with self._lock:
                self._queue.extendleft(reversed(remaining))

                while len(self._queue) > QUEUE_SIZE:
                    self._queue.popleft()
                    self.dropped += 1

            self._failed(self.clock(), reason)
            return

    def heartbeat_due(self):
        now = self.clock()

        return now >= self._next_attempt and (
            self._last_success is None or now - self._last_success >= HEARTBEAT_INTERVAL
        )

    def heartbeat(self):
        if not self.enabled:
            return None

        outcome, reason = self._post(dict(self.head, events=[]))

        if outcome == "ok":
            self._succeeded(self.clock())
        elif outcome == "retry":
            self._failed(self.clock(), reason)
        else:
            self._warn(f"canlılık isteği reddedildi ({reason}).")
            # Yine de 5 dk boyunca tekrar deneme; yapılandırma düzelmeden sonuç değişmez.
            self._last_success = self.clock()

        return outcome

    def tick(self):
        """Arka plan döngüsünün bir adımı (testlerde doğrudan çağrılır)."""
        now = self.clock()

        if self.pending() and (
            self.pending() >= FLUSH_COUNT or self._last_flush is None or now - self._last_flush >= FLUSH_INTERVAL
        ):
            self.flush()
        elif self._last_flush is None:
            self._last_flush = now

        if self.heartbeat_due():
            self.heartbeat()

    # ------------------------------------------------------------------ #
    # Thread
    # ------------------------------------------------------------------ #

    def start(self):
        if self._thread is None and self.enabled:
            self._thread = threading.Thread(target=self._run, name="vitalwatch-agent-sender", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — gönderim thread'i ölmesin
                self._warn(f"gönderim hatası: {error}")

            self._wake.wait(1.0)
            self._wake.clear()

    def stop(self, timeout=5.0):
        """Durdurur; kuyrukta kalanları bir kez göndermeyi dener."""
        self._stop.set()
        self._wake.set()

        if self._thread is not None:
            self._thread.join(timeout)

        if self.enabled and self.pending():
            self.flush(force=True)
