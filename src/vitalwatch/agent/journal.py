"""journald: ``journalctl -f -o json``.

Varsayılan olarak yalnızca üç şey aranır — bunlar başka hiçbir logda yok:

- başarısız systemd birimi (``x.service: Failed with result '…'``)
- OOM killer (``Out of memory: Killed process …``, systemd-oomd)
- çekirdek: segfault, genel koruma hatası, disk G/Ç hatası, kilitlenme

``journal_errors = on`` ile tüm ``err`` (öncelik ≤ 3) satırları da gelir;
gürültülüdür, varsayılan kapalı. Kimlik doğrulama ve posta servisleri o modda
da atlanır (her satır IP ya da adres taşır).

İmleç (``__CURSOR``) durum dosyasında: ajan yeniden başladığında kaldığı yerden
devam eder, aradaki olaylar kaybolmaz.
"""

import json
import re

from .normalize import build_event

FILE = "journald"

_OOM = re.compile(r"out of memory: Killed process (\d+) \(([^)]+)\)", re.IGNORECASE)
_SEGFAULT = re.compile(r"^(\S+?)\[(\d+)\]: segfault at")
_SEGFAULT_LIB = re.compile(r" in ([^\s\[]+)\[")
_TRAPS = re.compile(r"^traps: (\S+?)\[\d+\] (general protection fault|trap [\w ]+?)(?= ip:| sp:| in |$)")
_KERNEL_ERROR = re.compile(
    r"I/O error|EXT4-fs error|XFS \(.*\): (?:.*error|.*[Cc]orruption)|Btrfs.*error|Kernel panic"
    r"|BUG: |soft lockup|blocked for more than \d+ seconds|Hardware Error|Remounting filesystem read-only"
)
_UNIT_FAILED = re.compile(r"^(\S+\.(?:service|socket|mount|timer|path|swap|target|automount)): Failed with result '([^']+)'")

#: Tüm-err modunda da atlananlar: kimlik doğrulama ve posta (IP, adres taşır),
#: ajanın kendisi (kendi hatasını raporlamaz).
_EXCLUDED_IDENTS = re.compile(
    r"ssh|sudo|^su$|login|pam|postfix|dovecot|exim|sendmail|opendkim|fail2ban|polkit|vitalwatch-agent",
    re.IGNORECASE,
)


def _message(entry):
    value = entry.get("MESSAGE")

    if isinstance(value, list):
        # journald UTF-8 olmayan mesajı bayt dizisi olarak verir.
        try:
            value = bytes(value).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            return ""

    return str(value or "")[:4000]


def _priority(entry):
    try:
        return int(entry.get("PRIORITY", 6))
    except (TypeError, ValueError):
        return 6


def analyze(entry, all_errors=False):
    """Bir journald kaydından olay; ilgisizse ``None``."""
    if not isinstance(entry, dict):
        return None

    message = _message(entry).strip()

    if not message:
        return None

    ident = str(entry.get("SYSLOG_IDENTIFIER") or "")
    unit = str(entry.get("_SYSTEMD_UNIT") or entry.get("UNIT") or "")
    kernel = entry.get("_TRANSPORT") == "kernel" or ident == "kernel"

    if "vitalwatch-agent" in unit:
        return None

    match = _OOM.search(message)

    if match and (kernel or ident == "kernel"):
        name = match.group(2)

        return build_event("oom", f"Out of memory: Killed process <n> ({name})", FILE, command=name)

    if ident == "systemd-oomd" and "Killed" in message:
        return build_event("oom", message, FILE, command=entry.get("UNIT") or None)

    if kernel:
        match = _SEGFAULT.match(message)

        if match:
            name = match.group(1)
            library = _SEGFAULT_LIB.search(message)
            text = f"{name}[<n>]: segfault" + (f" in {library.group(1)}" if library else "")

            return build_event("kernel", text, FILE, command=name)

        match = _TRAPS.match(message)

        if match:
            return build_event("kernel", f"{match.group(1)}[<n>]: {match.group(2)}", FILE, command=match.group(1))

        if _KERNEL_ERROR.search(message):
            return build_event("kernel", message, FILE)

    # PID 1'in mesajları; kullanıcı yöneticisi (systemd --user) USER_UNIT taşır.
    if ident == "systemd" and "USER_UNIT" not in entry and "_SYSTEMD_USER_UNIT" not in entry:
        match = _UNIT_FAILED.match(message)

        if match:
            failed_unit = match.group(1)

            if "vitalwatch-agent" in failed_unit:
                return None

            return build_event(
                "service_failed",
                f"{failed_unit}: Failed with result '{match.group(2)}'",
                FILE,
                command=failed_unit,
            )

    if all_errors and _priority(entry) <= 3 and not _EXCLUDED_IDENTS.search(ident or unit):
        return build_event("kernel" if kernel else "log_error", message, FILE, command=unit or ident or None)

    return None


class JournalWatcher:
    """``LineProcess``'e satır işleyici ve komut sağlar; imleci tutar."""

    def __init__(self, emit, cursor=None, all_errors=False, on_unit_failed=None):
        self.emit = emit
        self.cursor = cursor if isinstance(cursor, str) and cursor else None
        self.all_errors = all_errors
        self.on_unit_failed = on_unit_failed

    def command(self):
        command = ["journalctl", "-f", "-o", "json", "--no-pager", "-q"]

        # İmleç yoksa yalnızca yeni kayıtlar: geçmiş içe aktarılmaz.
        return command + (["--after-cursor", self.cursor] if self.cursor else ["-n", "0"])

    def handle_line(self, line):
        line = line.strip()

        if not line.startswith("{"):
            return

        try:
            entry = json.loads(line)
        except ValueError:
            return

        if not isinstance(entry, dict):
            return

        cursor = entry.get("__CURSOR")

        if isinstance(cursor, str) and cursor:
            self.cursor = cursor

        event = analyze(entry, self.all_errors)

        if event is None:
            return

        if event["kind"] == "service_failed" and self.on_unit_failed and event.get("command"):
            self.on_unit_failed(event["command"])

        self.emit(event)
