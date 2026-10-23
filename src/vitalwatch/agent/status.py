"""Sunucu durum özeti (``server_status``) — hub'ın günlük raporu ve ``/server`` komutu için.

Her 5 dakikada bir **tek, en son** özet gönderilir; hub yalnızca sonuncuyu
saklar. Zaman serisi, grafik ya da süreç listesi değildir.

Sözleşme (``v: 1``)::

    v, collected_at, hostname, uptime_s, load, cpus,
    memory {total_mb, available_mb, swap_total_mb, swap_used_mb},
    disks [{mount, used_pct, inodes_pct, free_gb}],
    failed_units [..], containers [{name, state, health, restarts, started_at}],
    updates {pending, security, reboot_required}, (truncated)

Kurallar:

- Her alan ayrı toplanır; toplayıcısı hata veren alan ``null`` olur, özet
  yine gönderilebilir. Bu modül hiçbir koşulda fırlatmaz.
- IP adresi hiçbir alana girmez (CLAUDE.md, "Toplanmayacaklar"). Yalnızca
  kısa makine adı, birim adı, konteyner adı ve bağlama noktası.
- Güncelleme sayımı (apt) pahalıdır: 1 saat önbelleklenir, alt süreçler zaman
  aşımıyla çalışır.
- Gövde 7000 baytı aşarsa sondan kırpılır (önce konteynerler, sonra
  başarısız birimler, sonra diskler) ve ``truncated: true`` eklenir. Hub
  8192 baytı aşan gövdeyi okumadan atar.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time

from .system import FailedUnitsChecker, real_mounts, usage

VERSION = 1

#: Gönderim aralığı (saniye).
STATUS_INTERVAL = 300.0

#: Gövdenin hedef üst sınırı; hub sınırı 8192 (``IngestController::MAX_BODY_BYTES``).
STATUS_BODY_LIMIT = 7000

MAX_DISKS = 10
MAX_UNITS = 20
MAX_CONTAINERS = 30
MAX_NAME = 64

UPDATES_CACHE_SECONDS = 3600.0
APT_TIMEOUT = 60
COMMAND_TIMEOUT = 20

APT_CHECK = "/usr/lib/update-notifier/apt-check"
APT_SIMULATE = ["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"]
REBOOT_REQUIRED = "/var/run/reboot-required"

#: Docker'ın "hiç başlamadı" zamanı.
_ZERO_TIME = "0001-01-01T00:00:00Z"


# ---------------------------------------------------------------------- #
# Ayrıştırıcılar — saf fonksiyonlar, testler doğrudan çağırır
# ---------------------------------------------------------------------- #


def parse_uptime(text):
    """``/proc/uptime`` → saniye (tam sayı)."""
    return int(float(text.split()[0]))


def parse_meminfo(text):
    """``/proc/meminfo`` → MB cinsinden bellek özeti. Eksik alan ``ValueError``."""
    values = {}

    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()

        if parts and parts[0].isdigit():
            values[name.strip()] = int(parts[0])  # kB

    def mb(name):
        if name not in values:
            raise ValueError(f"{name} missing")
        return values[name] // 1024

    swap_total = mb("SwapTotal")

    return {
        "total_mb": mb("MemTotal"),
        "available_mb": mb("MemAvailable"),
        "swap_total_mb": swap_total,
        "swap_used_mb": max(0, swap_total - mb("SwapFree")),
    }


def disk_entry(mount_point, stats):
    """Tek bağlama noktasının özeti; doluluk hesaplanamıyorsa ``None``."""
    disk, inodes = usage(stats)

    if disk is None:
        return None

    return {
        "mount": mount_point[:MAX_NAME],
        "used_pct": round(disk, 1),
        "inodes_pct": round(inodes, 1) if inodes is not None else None,
        "free_gb": round(stats.f_bavail * stats.f_frsize / 1e9, 1),
    }


def _iso(value):
    """Docker zamanı → saniye hassasiyetinde ISO; sıfır zaman ``None``."""
    if not isinstance(value, str) or not value or value.startswith("0001-01-01"):
        return None

    # Nanosaniye kesri atılır: daha kısa ve PHP tarafında ayrıştırması kolay.
    return re.sub(r"\.\d+(?=Z|[+-]\d\d:?\d\d$)", "", value)[:40]


def _text(value, limit=32):
    return str(value)[:limit] if isinstance(value, str) and value else None


def parse_docker_inspect(text):
    """``docker inspect`` JSON çıktısı → konteyner listesi.

    Sorunlu olanlar (çalışmayan ya da sağlıksız) öne alınır: gövde
    kırpılırsa önce sağlıklı konteynerler düşer.
    """
    data = json.loads(text)
    containers = []

    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue

        state = item.get("State") if isinstance(item.get("State"), dict) else {}
        health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
        restarts = item.get("RestartCount")

        containers.append(
            {
                "name": str(item.get("Name") or item.get("Id", "")[:12] or "container").lstrip("/")[:MAX_NAME],
                "state": _text(state.get("Status")),
                "health": _text(health.get("Status")),
                "restarts": restarts if isinstance(restarts, int) and not isinstance(restarts, bool) else 0,
                "started_at": _iso(state.get("StartedAt")),
            }
        )

    def problem_first(container):
        healthy = container["state"] == "running" and container["health"] in (None, "healthy", "starting")
        return (healthy, container["name"])

    containers.sort(key=problem_first)

    return containers[:MAX_CONTAINERS]


def parse_apt_check(stderr):
    """``apt-check`` stderr'i (``"10;3"``) → ``(bekleyen, güvenlik)``; tanınmazsa ``None``."""
    for line in reversed(stderr.strip().splitlines()):
        match = re.fullmatch(r"\s*(\d+);(\d+)\s*", line)

        if match:
            return int(match.group(1)), int(match.group(2))

    return None


def parse_apt_simulate(stdout):
    """``apt-get -s upgrade`` çıktısı → ``(bekleyen, güvenlik)``."""
    pending = security = 0

    for line in stdout.splitlines():
        if line.startswith("Inst "):
            pending += 1
            if "-security" in line:
                security += 1

    return pending, security


def _size(body):
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def status_body(head, status, limit=STATUS_BODY_LIMIT):
    """Gönderilecek gövde: ``dict(head, events=[], server_status=…)``, sınıra sığdırılmış.

    Sığmazsa sırasıyla ``containers``, ``failed_units``, ``disks`` sondan
    kırpılır ve ``truncated: true`` eklenir. Girdi değiştirilmez.
    """
    status = dict(status)
    body = dict(head, events=[], server_status=status)

    if _size(body) <= limit:
        return body

    status["truncated"] = True

    for field in ("containers", "failed_units", "disks"):
        if not isinstance(status.get(field), list):
            continue

        items = list(status[field])
        status[field] = items

        while items and _size(body) > limit:
            items.pop()

        if _size(body) <= limit:
            break

    return body


# ---------------------------------------------------------------------- #
# Toplayıcı
# ---------------------------------------------------------------------- #


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


class StatusCollector:
    """Özet toplar. Her alan bağımsız; hata veren alan ``None`` olur.

    :param docker_mode: ajan yapılandırmasındaki ``docker`` kaynağı
        (``auto``/``on``/``off``) — docker izleyicisiyle aynı kural: ``off``
        ise ya da ``docker`` yoksa ``containers`` ``None``.
    """

    def __init__(
        self,
        docker_mode="auto",
        runner=subprocess.run,
        reader=_read,
        statvfs=os.statvfs,
        which=shutil.which,
        exists=os.path.exists,
        clock=time.monotonic,
        wall_clock=time.time,
        hostname=socket.gethostname,
        loadavg=os.getloadavg,
        cpu_count=os.cpu_count,
    ):
        self.docker_mode = docker_mode
        self.runner = runner
        self.reader = reader
        self.statvfs = statvfs
        self.which = which
        self.exists = exists
        self.clock = clock
        self.wall_clock = wall_clock
        self.hostname_func = hostname
        self.loadavg = loadavg
        self.cpu_count = cpu_count
        self._updates = None
        self._updates_at = None

    # -- alanlar ------------------------------------------------------- #

    def hostname(self):
        name = (self.hostname_func() or "").split(".")[0][:MAX_NAME]
        return name or None

    def uptime(self):
        return parse_uptime(self.reader("/proc/uptime"))

    def load(self):
        return [round(value, 2) for value in self.loadavg()]

    def cpus(self):
        return self.cpu_count()

    def memory(self):
        return parse_meminfo(self.reader("/proc/meminfo"))

    def disks(self):
        entries = []

        for mount_point in real_mounts(self.reader("/proc/mounts")):
            try:
                entry = disk_entry(mount_point, self.statvfs(mount_point))
            except OSError:
                continue

            if entry:
                entries.append(entry)

            if len(entries) >= MAX_DISKS:
                break

        return entries

    def _run(self, command, timeout=COMMAND_TIMEOUT):
        """Tamamlanan süreç; komut yoksa ``None``. Diğer hatalar fırlar (alan ``None`` olur)."""
        env = dict(os.environ, LC_ALL="C", LANG="C")

        try:
            return self.runner(command, capture_output=True, text=True, timeout=timeout, env=env)
        except FileNotFoundError:
            return None

    def failed_units(self):
        result = self._run(FailedUnitsChecker.COMMAND)

        if result is None or result.returncode != 0:
            return None

        units = [unit for unit in FailedUnitsChecker.parse(result.stdout) if "vitalwatch-agent" not in unit]

        return [unit[:MAX_NAME] for unit in units[:MAX_UNITS]]

    def docker_enabled(self):
        return self.docker_mode != "off" and self.which("docker") is not None

    def containers(self):
        if not self.docker_enabled():
            return None

        listed = self._run(["docker", "ps", "-aq"])

        if listed is None or listed.returncode != 0:
            return None

        ids = listed.stdout.split()

        if not ids:
            return []

        inspected = self._run(["docker", "inspect"] + ids)

        if inspected is None or inspected.returncode != 0:
            return None

        return parse_docker_inspect(inspected.stdout)

    def update_method(self):
        """``apt-check``, ``apt-get`` ya da ``None`` (apt olmayan sistem)."""
        if self.exists(APT_CHECK):
            return "apt-check"

        if self.which("apt-get") is not None:
            return "apt-get"

        return None

    def _count_updates(self):
        method = self.update_method()

        if method is None:
            return None

        counts = None

        if method == "apt-check":
            result = self._run([APT_CHECK], timeout=APT_TIMEOUT)

            if result is not None:
                counts = parse_apt_check(result.stderr or "")

        if counts is None and self.which("apt-get") is not None:
            result = self._run(APT_SIMULATE, timeout=APT_TIMEOUT)

            if result is not None and result.returncode == 0:
                counts = parse_apt_simulate(result.stdout)

        return counts

    def updates(self):
        # Pahalı kısım (sayım) saatte bir; yeniden başlatma bayrağı her seferinde.
        now = self.clock()

        if self._updates_at is None or now - self._updates_at >= UPDATES_CACHE_SECONDS:
            # Hata da önbelleklenir: başarısız apt her 5 dakikada 60 sn beklemesin.
            self._updates_at = now

            try:
                self._updates = self._count_updates()
            except Exception:  # noqa: BLE001
                self._updates = None

        if self._updates is None:
            return None

        pending, security = self._updates

        return {"pending": pending, "security": security, "reboot_required": bool(self.exists(REBOOT_REQUIRED))}

    # -- özet ---------------------------------------------------------- #

    FIELDS = ("hostname", "uptime_s", "load", "cpus", "memory", "disks", "failed_units", "containers", "updates")

    def collect(self):
        """Tek özet. Asla fırlatmaz."""
        try:
            collected_at = int(self.wall_clock())
        except Exception:  # noqa: BLE001
            collected_at = int(time.time())

        snapshot = {"v": VERSION, "collected_at": collected_at}
        methods = {
            "hostname": self.hostname,
            "uptime_s": self.uptime,
            "load": self.load,
            "cpus": self.cpus,
            "memory": self.memory,
            "disks": self.disks,
            "failed_units": self.failed_units,
            "containers": self.containers,
            "updates": self.updates,
        }

        for field in self.FIELDS:
            try:
                snapshot[field] = methods[field]()
            except Exception:  # noqa: BLE001 — tek alan özeti bozmasın
                snapshot[field] = None

        return snapshot


def collector_for(config):
    """Ajan yapılandırmasına göre toplayıcı."""
    return StatusCollector(docker_mode=config.source_mode("docker"))
