"""Ajanın gövdesi: kaynakları kurar, döngüyü çevirir, temiz kapanır.

Thread düzeni:

- ana thread: dosya yoklama (1 sn), glob yeniden tarama (60 sn), durum kaydı
  (10 sn) ve sinyaller;
- ``sender``: gönderim, geri çekilme, canlılık;
- her alt süreç kaynağı (journald, docker events, her ``docker logs``) kendi
  thread'inde — biri beklerken diğerleri durmaz;
- ``system``: ``systemctl --failed`` ve disk denetimi (5 dk).

SIGTERM/SIGINT durdurma bayrağını kaldırır; ana thread alt süreçleri kapatır,
kuyruğu bir kez daha göndermeyi dener ve durumu kaydeder.
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import threading
import time

from ..client import HubClient
from . import docker as docker_source
from .journal import JournalWatcher
from .parsers import FILE_SOURCES, make_custom_parser
from .process import LineProcess, warn
from .sender import Sender
from .state import StateStore
from .system import DiskChecker, FailedUnitsChecker, real_mounts, usage
from .tail import Tailer, _skip_name

POLL_INTERVAL = 1.0
RESCAN_INTERVAL = 60.0
STATE_SAVE_INTERVAL = 10.0
SYSTEM_INTERVAL = 300.0
STATE_VERSION = 1


def file_sources(config):
    """``[(glob, parser, kaynak_adı), …]`` — kapalı kaynaklar hariç, özel loglar dahil."""
    sources = []

    for name, entries in FILE_SOURCES:
        if config.source_mode(name) != "off":
            sources.extend((pattern, parser, name) for pattern, parser in entries)

    for custom in config.custom_logs:
        parser = make_custom_parser(custom["pattern"], custom["kind"])
        sources.append((custom["path"], parser, f"log:{custom['name']}"))

    return sources


class Agent:
    def __init__(self, config, store=None, client=None, warn_func=None):
        self.config = config
        self.warn = warn_func or warn
        self.ignore = config.ignore_rules()
        self.client = client or HubClient(url=config.url, key=config.key, secret=config.secret, timeout=config.timeout)
        self.sender = Sender(self.client, env=config.env, enabled=config.enabled, warn=self.warn)
        self.store = store or StateStore()
        self.stop_event = threading.Event()
        self.tailer = None
        self.journal = None
        self.failed_units = None
        self.disk = None
        self._processes = []
        self._system_thread = None
        self._last_saved = None

    # ------------------------------------------------------------------ #
    # Olay yolu
    # ------------------------------------------------------------------ #

    def handle_line(self, line, parser, file):
        """Dosyadan gelen ham satır. Yok sayma ham satırda: ``host:`` alanı normalize sonrası yoktur."""
        if self.ignore.matches_line(line):
            return

        try:
            event = parser(line, file)
        except Exception:  # noqa: BLE001 — bozuk satır ajanı düşürmesin
            return

        self.emit(event)

    def emit(self, event):
        if not event:
            return

        if self.ignore.matches_event(event) or self.ignore.matches_line(event.get("msg") or ""):
            return

        self.sender.submit(event)

    # ------------------------------------------------------------------ #
    # Kurulum
    # ------------------------------------------------------------------ #

    def _enabled(self, name, binary):
        mode = self.config.source_mode(name)

        if mode == "off":
            return False

        if shutil.which(binary) is None:
            if mode == "on":
                self.warn(f"{binary} not found; {name} source disabled.")
            return False

        return True

    def setup(self, state):
        self.tailer = Tailer(file_sources(self.config), saved=state.get("files"), warn=self.warn)
        self.tailer.scan()

        if self._enabled("systemd", "systemctl"):
            self.failed_units = FailedUnitsChecker(self.emit, known=state.get("failed_units"), warn=self.warn)

        if self.config.source_mode("disk") != "off":
            self.disk = DiskChecker(self.emit, threshold=self.config.disk_threshold, known=state.get("disk_alerts"))

        if self._enabled("journal", "journalctl"):
            self.journal = JournalWatcher(
                self.emit,
                cursor=state.get("journal_cursor"),
                all_errors=self.config.journal_errors,
                on_unit_failed=self.failed_units.note_failed if self.failed_units else None,
            )
            self._processes.append(
                LineProcess("journal", self.journal.command, self.journal.handle_line, self.stop_event, self.warn)
            )

        if self._enabled("docker", "docker"):
            watcher = docker_source.DockerWatcher(self.emit)
            self._processes.append(
                LineProcess("docker", docker_source.events_command(), watcher.handle_line, self.stop_event, self.warn)
            )

            for name in self.config.docker_logs:
                log_watcher = docker_source.DockerLogWatcher(name, self.emit)
                self._processes.append(
                    LineProcess(f"docker-logs:{name}", log_watcher.command, log_watcher.handle_line, self.stop_event, self.warn)
                )

    def state(self):
        return {
            "version": STATE_VERSION,
            "files": self.tailer.state() if self.tailer else {},
            "journal_cursor": self.journal.cursor if self.journal else None,
            "failed_units": self.failed_units.known() if self.failed_units else [],
            "disk_alerts": self.disk.known() if self.disk else {},
        }

    def save_state(self, force=False):
        data = self.state()
        encoded = json.dumps(data, sort_keys=True)

        if force or encoded != self._last_saved:
            if self.store.save(data):
                self._last_saved = encoded

    # ------------------------------------------------------------------ #
    # Döngü
    # ------------------------------------------------------------------ #

    def _system_loop(self):
        while not self.stop_event.is_set():
            for checker in (self.failed_units, self.disk):
                if checker is None:
                    continue
                try:
                    checker.check()
                except Exception as error:  # noqa: BLE001
                    self.warn(f"periodic check error: {error}")

            self.stop_event.wait(SYSTEM_INTERVAL)

    def poll_files(self):
        for path, line, parser in self.tailer.poll():
            self.handle_line(line, parser, path)

    def request_stop(self, *_):
        self.stop_event.set()

    def run(self, install_signals=True):
        if install_signals:
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)

        self.setup(self.store.load())
        self.sender.start()

        for process in self._processes:
            process.start()

        self._system_thread = threading.Thread(target=self._system_loop, name="vitalwatch-agent-system", daemon=True)
        self._system_thread.start()

        last_scan = last_save = time.monotonic()

        try:
            while not self.stop_event.wait(POLL_INTERVAL):
                try:
                    self.poll_files()
                    now = time.monotonic()

                    if now - last_scan >= RESCAN_INTERVAL:
                        self.tailer.scan()
                        last_scan = now

                    if now - last_save >= STATE_SAVE_INTERVAL:
                        self.save_state()
                        last_save = now
                except Exception as error:  # noqa: BLE001 — döngü ölmesin
                    self.warn(f"loop error: {error}")
        finally:
            self.shutdown()

    def shutdown(self):
        self.stop_event.set()

        for process in self._processes:
            process.stop()

        for process in self._processes:
            process.join(timeout=3)

        self.sender.stop(timeout=5)
        self.save_state(force=True)


# ---------------------------------------------------------------------- #
# Teşhis (check)
# ---------------------------------------------------------------------- #

FOUND = "found"
NOT_FOUND = "not found"
NOT_READABLE = "not readable"
DISABLED = "disabled"


def _files(count):
    return f"{count} file" + ("" if count == 1 else "s")


def _files_status(patterns):
    files = sorted({path for pattern in patterns for path in glob.glob(pattern) if not _skip_name(path) and os.path.isfile(path)})

    if not files:
        return NOT_FOUND, ""

    readable = [path for path in files if os.access(path, os.R_OK)]

    if not readable:
        return NOT_READABLE, f"{_files(len(files))}, none readable"

    detail = _files(len(readable)) + (f", {len(files) - len(readable)} not readable" if len(readable) < len(files) else "")

    return FOUND, detail


def _run(command, timeout=10):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))

    return result


def source_report(config):
    """``[(kaynak, durum, ayrıntı), …]`` — ``check`` komutunun tablosu."""
    rows = []

    for name, entries in FILE_SOURCES:
        if config.source_mode(name) == "off":
            rows.append((name, DISABLED, ""))
            continue

        status, detail = _files_status([pattern for pattern, _ in entries])
        rows.append((name, status, detail))

    for custom in config.custom_logs:
        status, detail = _files_status([custom["path"]])
        rows.append((f"log:{custom['name']}", status, detail or custom["path"]))

    # journald
    if config.source_mode("journal") == "off":
        rows.append(("journal", DISABLED, ""))
    else:
        result = _run(["journalctl", "-n", "1", "-q", "--no-pager", "-o", "json"])

        if result is None:
            rows.append(("journal", NOT_FOUND, "journalctl not installed"))
        elif result.returncode != 0 or "insufficient permissions" in result.stderr or "not seeing messages" in result.stderr:
            rows.append(("journal", NOT_READABLE, (result.stderr.strip().splitlines() or [""])[0][:120]))
        else:
            rows.append(("journal", FOUND, "all err-level lines enabled" if config.journal_errors else ""))

    # systemd
    if config.source_mode("systemd") == "off":
        rows.append(("systemd", DISABLED, ""))
    else:
        result = _run(FailedUnitsChecker.COMMAND)

        if result is None:
            rows.append(("systemd", NOT_FOUND, "systemctl not installed"))
        elif result.returncode != 0:
            rows.append(("systemd", NOT_READABLE, result.stderr.strip()[:120]))
        else:
            failed = FailedUnitsChecker.parse(result.stdout)
            rows.append(("systemd", FOUND, f"currently failed: {', '.join(failed)}" if failed else "no failed units"))

    # docker
    if config.source_mode("docker") == "off":
        rows.append(("docker", DISABLED, ""))
    else:
        result = _run(["docker", "info", "--format", "{{.ServerVersion}}"])

        if result is None:
            rows.append(("docker", NOT_FOUND, "docker not installed"))
        elif result.returncode != 0:
            message = result.stderr.strip()
            status = NOT_READABLE if "permission denied" in message.lower() else NOT_FOUND
            rows.append(("docker", status, message.splitlines()[0][:120] if message else ""))
        else:
            logs = f", logs: {', '.join(config.docker_logs)}" if config.docker_logs else ""
            rows.append(("docker", FOUND, f"version {result.stdout.strip()}{logs}"))

    # disk
    if config.source_mode("disk") == "off":
        rows.append(("disk", DISABLED, ""))
    else:
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
                mounts = real_mounts(handle.read())
        except OSError:
            mounts = None

        if mounts is None:
            rows.append(("disk", NOT_READABLE, "/proc/mounts"))
        else:
            parts = []

            for mount_point in mounts:
                try:
                    disk, _ = usage(os.statvfs(mount_point))
                except OSError:
                    continue
                if disk is not None:
                    parts.append(f"{mount_point} {int(disk)}%")

            rows.append(("disk", FOUND, f"threshold {config.disk_threshold}%; " + ", ".join(parts)))

    return rows
