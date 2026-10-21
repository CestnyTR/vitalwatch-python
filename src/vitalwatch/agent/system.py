"""Periyodik denetimler: ``systemctl --failed`` ve disk doluluğu.

Bunlar **olay** düzeyindedir: eşik aşıldığında tek olay. CPU/RAM/disk grafiği,
sürekli metrik ve süreç listesi kapsam dışıdır (sözleşme, "Kapsam kararı").
"""

import math
import os
import re
import subprocess
import threading
import time

from .normalize import build_event

#: Gerçek (blok aygıtlı ya da ağ) dosya sistemleri. tmpfs, overlay, squashfs
#: (snap paketleri hep %100 dolu görünür) ve sözde dosya sistemleri dışarıda.
REAL_FILESYSTEMS = frozenset(
    (
        "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
        "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "nfs", "nfs4", "cifs", "smb3",
    )
)

DISK_DEDUPE_SECONDS = 3600.0


def _unescape(value):
    """/proc/mounts boşluğu ``\\040`` olarak yazar."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def real_mounts(text):
    """``/proc/mounts`` içeriğinden izlenecek bağlama noktaları.

    Aynı aygıt birden çok yere bağlanmışsa (bind mount, Docker birimleri)
    yalnızca ilki alınır — aynı disk için birden fazla olay çıkmasın.
    """
    seen_devices = set()
    mounts = []

    for line in text.splitlines():
        parts = line.split()

        if len(parts) < 3 or parts[2] not in REAL_FILESYSTEMS:
            continue

        device, mount_point = parts[0], _unescape(parts[1])

        if device in seen_devices or mount_point.startswith(("/snap/", "/proc", "/sys", "/dev", "/run")):
            continue

        seen_devices.add(device)
        mounts.append(mount_point)

    return mounts


def _read_mounts():
    with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def usage(stats):
    """``(disk_yüzdesi, inode_yüzdesi)``; hesaplanamayan ``None``. ``df`` ile aynı formül."""
    disk = inodes = None
    used = stats.f_blocks - stats.f_bfree
    total = used + stats.f_bavail

    if stats.f_blocks > 0 and total > 0:
        disk = used * 100.0 / total

    # btrfs gibi dosya sistemleri inode sayısı bildirmez (f_files = 0).
    if stats.f_files > 0:
        inodes = (stats.f_files - stats.f_ffree) * 100.0 / stats.f_files

    return disk, inodes


class DiskChecker:
    def __init__(self, emit, threshold=90, statvfs=os.statvfs, mounts_reader=_read_mounts, clock=time.monotonic):
        self.emit = emit
        self.threshold = threshold
        self.statvfs = statvfs
        self.mounts_reader = mounts_reader
        self.clock = clock
        self._last = {}

    def check(self):
        try:
            mounts = real_mounts(self.mounts_reader())
        except OSError:
            return

        now = self.clock()

        for mount_point in mounts:
            try:
                disk, inodes = usage(self.statvfs(mount_point))
            except OSError:
                continue

            for kind, percent, text in (
                ("disk", disk, "disk {}% full"),
                ("inodes", inodes, "inodes {}% used"),
            ):
                if percent is None or percent < self.threshold:
                    continue

                last = self._last.get((mount_point, kind))

                if last is not None and now - last < DISK_DEDUPE_SECONDS:
                    continue

                self._last[(mount_point, kind)] = now
                event = build_event("disk_full", f"{mount_point}: " + text.format(int(math.ceil(percent))), "disk")

                if event:
                    self.emit(event)


class FailedUnitsChecker:
    """``systemctl --failed`` — ajan kapalıyken düşen birimleri de yakalar.

    Aynı birim başarısız kaldığı sürece bir kez raporlanır; düzelip yeniden
    düşerse tekrar. journald zaten raporladıysa (``note_failed``) burada
    ikinci kez gönderilmez.
    """

    COMMAND = ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"]

    def __init__(self, emit, known=None, runner=subprocess.run, warn=None):
        self.emit = emit
        self.runner = runner
        self.warn = warn
        self.available = True
        self._known = set(known or ())
        self._lock = threading.Lock()

    def note_failed(self, unit):
        with self._lock:
            self._known.add(unit)

    def known(self):
        with self._lock:
            return sorted(self._known)

    @staticmethod
    def parse(output):
        units = []

        for line in output.splitlines():
            parts = line.replace("●", " ").replace("*", " ").split()

            if parts and "." in parts[0]:
                units.append(parts[0])

        return units

    def check(self):
        if not self.available:
            return

        try:
            result = self.runner(self.COMMAND, capture_output=True, text=True, timeout=20)
        except FileNotFoundError:
            self.available = False
            if self.warn:
                self.warn("systemctl bulunamadı; systemd kaynağı kapalı.")
            return
        except (OSError, subprocess.SubprocessError):
            return

        if result.returncode != 0:
            return

        current = set(self.parse(result.stdout))

        with self._lock:
            new = sorted(current - self._known)
            self._known = current

        for unit in new:
            if "vitalwatch-agent" in unit:
                continue

            event = build_event("service_failed", f"{unit}: failed (systemctl --failed)", "systemd", command=unit)

            if event:
                self.emit(event)
