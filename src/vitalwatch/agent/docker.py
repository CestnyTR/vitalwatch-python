"""Docker: ``docker events`` ve isteğe bağlı ``docker logs -f``.

Olaylar:

- ``die`` ve çıkış kodu ≠ 0 → ``api: exited with code 137``
- 10 dakikada ≥ 3 ``die`` → ``api: restart loop (4 in 10 min)`` (konteyner
  başına 10 dakikada bir kez)
- ``oom`` → ``api: killed by OOM``
- ``health_status: unhealthy`` → ``api: unhealthy``

``docker stop`` / ``docker compose down`` da ``die`` üretir (çıkış kodu 143
ya da 137): hemen önce gelen ``kill`` / ``stop`` olayı görülürse kasıtlı
durdurma sayılır, raporlanmaz ve döngü sayacına girmez — yoksa her deploy
bir hata bırakırdı.
"""

import collections
import json
import re
import time

from .normalize import build_event, strip_prefix

FILE = "docker"

LOOP_WINDOW = 600.0
LOOP_COUNT = 3

#: ``kill``/``stop`` ile ``die`` arasındaki en uzun süre (docker stop varsayılan
#: zaman aşımı 10 sn; geniş tutuldu).
STOP_GRACE = 60.0

#: ``docker logs`` satırlarında hata sayılan desen.
DEFAULT_LOG_PATTERN = re.compile(r"\b(?:ERROR|CRITICAL|FATAL|PANIC|Traceback|Exception|panic:)\b")


def events_command():
    return ["docker", "events", "--format", "{{json .}}", "--filter", "type=container"]


def logs_command(name):
    return ["docker", "logs", "-f", "--tail", "0", name]


class DockerWatcher:
    def __init__(self, emit, clock=time.time):
        self.emit = emit
        self.clock = clock
        self._dies = collections.defaultdict(collections.deque)
        self._last_loop = {}
        self._stopping = {}

    def handle_line(self, line):
        line = line.strip()

        if not line.startswith("{"):
            return

        try:
            entry = json.loads(line)
        except ValueError:
            return

        for event in self.analyze(entry):
            self.emit(event)

    @staticmethod
    def _time(entry, fallback):
        try:
            if entry.get("timeNano"):
                return int(entry["timeNano"]) / 1e9
            if entry.get("time"):
                return float(entry["time"])
        except (TypeError, ValueError):
            pass

        return fallback

    def analyze(self, entry):
        if not isinstance(entry, dict):
            return []

        if (entry.get("Type") or "container") != "container":
            return []

        action = str(entry.get("Action") or entry.get("status") or "")
        actor = entry.get("Actor") if isinstance(entry.get("Actor"), dict) else {}
        attributes = actor.get("Attributes") if isinstance(actor.get("Attributes"), dict) else {}
        name = str(attributes.get("name") or (actor.get("ID") or entry.get("id") or "")[:12] or "container")
        now = self._time(entry, self.clock())

        if action in ("kill", "stop"):
            self._stopping[name] = now
            return []

        if action == "oom":
            return [build_event("container_failed", f"{name}: killed by OOM", FILE, command=name)]

        if action.startswith("health_status") and action.split(":", 1)[-1].strip() == "unhealthy":
            return [build_event("container_failed", f"{name}: unhealthy", FILE, command=name)]

        if action != "die":
            return []

        stopped_at = self._stopping.pop(name, None)

        if stopped_at is not None and 0 <= now - stopped_at <= STOP_GRACE:
            return []

        events = []
        code = str(attributes.get("exitCode", "")).strip()

        if code and code != "0":
            events.append(build_event("container_failed", f"{name}: exited with code {code}", FILE, command=name))

        dies = self._dies[name]
        dies.append(now)

        while dies and now - dies[0] > LOOP_WINDOW:
            dies.popleft()

        last = self._last_loop.get(name)

        if len(dies) >= LOOP_COUNT and (last is None or now - last >= LOOP_WINDOW):
            self._last_loop[name] = now
            events.append(
                build_event(
                    "container_failed",
                    f"{name}: restart loop ({len(dies)} in {int(LOOP_WINDOW // 60)} min)",
                    FILE,
                    command=name,
                )
            )

        return [event for event in events if event]


class DockerLogWatcher:
    """Seçilen konteynerin ``docker logs -f`` çıktısı; desen eşleşen satır olay olur."""

    def __init__(self, name, emit, pattern=None):
        self.name = name
        self.emit = emit
        self.pattern = pattern or DEFAULT_LOG_PATTERN

    def command(self):
        return logs_command(self.name)

    def handle_line(self, line):
        if not self.pattern.search(line):
            return

        event = build_event("log_error", strip_prefix(line), f"docker:{self.name}", command=self.name)

        if event:
            self.emit(event)
