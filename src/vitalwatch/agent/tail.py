"""Glob takip eden dosya okuyucu.

- Konum ``(inode, ofset)`` olarak tutulur. Inode değişti → log döndürüldü,
  yeni dosya baştan okunur. Boyut ofsetten küçük → kırpıldı (``copytruncate``),
  baştan okunur.
- Dosya her yoklamada açılıp kapatılır: Plesk'te yüzlerce site × iki log açık
  tutulsaydı servisin dosya tanıtıcı sınırı dolardı. Bedeli: döndürmeden hemen
  önce yazılıp henüz okunmamış birkaç satır kaybolabilir (yoklama 1 sn).
- İlk taramada bulunan, durumda kaydı olmayan dosya **sonundan** okunur —
  geçmiş içe aktarılmaz. Sonradan beliren dosya (yeni Plesk sitesi) baştan
  okunur; çok büyükse yine sonundan.
- Glob'lar periyodik yeniden taranır; ajan yeniden başlatılmadan yeni siteler
  izlenmeye başlar.
"""

import glob
import os
import re
import stat
import sys

#: Tek yoklamada bir dosyadan okunan en fazla bayt; kalanı sonraki yoklamada.
MAX_READ = 1024 * 1024

#: Satır sonu gelmeden bu uzunluğa ulaşan parça satır sayılır.
MAX_LINE = 64 * 1024

#: Sonradan beliren ya da ajan kapalıyken döndürülen dosya bu boyuttan
#: küçükse baştan okunur; büyükse sonundan (geçmiş içe aktarılmaz).
BACKLOG_LIMIT = 256 * 1024

#: Döndürülmüş / sıkıştırılmış kopyalar izlenmez: ``error_log-20261021``,
#: ``error.log.1``, ``*.gz``. ``/var/log/httpd/*error*`` gibi geniş glob'lar
#: bunları da yakalardı.
_ROTATED = re.compile(r"(?:\.(?:gz|bz2|xz|zst|zip|old|bak|\d+)|[-_.]\d{8,10})$")


def _skip_name(path):
    return bool(_ROTATED.search(path))


class Tailer:
    def __init__(self, sources, saved=None, warn=None):
        """:param sources: ``[(glob, parser, kaynak_adı), …]``
        :param saved: önceki durum ``{yol: {"inode": int, "offset": int}}``
        """
        self.sources = list(sources)
        self._saved = dict(saved or {})
        self._files = {}
        self._first_scan_done = False
        self._warned = set()
        self._warn = warn or (lambda message: print(f"vitalwatch-agent: {message}", file=sys.stderr, flush=True))

    # ------------------------------------------------------------------ #

    def scan(self):
        """Glob'ları yeniden çözer; yeni dosyaları ekler, kaybolanları bırakır."""
        found = {}

        for pattern, parser, name in self.sources:
            for path in sorted(glob.glob(pattern)):
                if path not in found and not _skip_name(path):
                    found[path] = (parser, name)

        for path in list(self._files):
            if path not in found:
                del self._files[path]

        for path, (parser, name) in found.items():
            if path in self._files:
                continue

            try:
                info = os.stat(path)
            except OSError:
                continue

            if not stat.S_ISREG(info.st_mode):
                continue

            if not os.access(path, os.R_OK):
                if path not in self._warned:
                    self._warned.add(path)
                    self._warn(f"{path} is not readable (permission denied), skipping.")
                continue

            self._files[path] = {
                "inode": info.st_ino,
                "offset": self._initial_offset(path, info),
                "parser": parser,
                "source": name,
            }

        self._first_scan_done = True

    def _initial_offset(self, path, info):
        saved = self._saved.pop(path, None)

        if isinstance(saved, dict) and saved.get("inode") == info.st_ino:
            offset = saved.get("offset")

            if isinstance(offset, int) and 0 <= offset <= info.st_size:
                return offset

            return 0  # kırpılmış

        if isinstance(saved, dict) or self._first_scan_done:
            # Ajan kapalıyken döndürüldü ya da sonradan belirdi.
            return 0 if info.st_size <= BACKLOG_LIMIT else info.st_size

        return info.st_size

    # ------------------------------------------------------------------ #

    def poll(self):
        """Yeni tam satırlar: ``[(yol, satır, parser), …]``."""
        lines = []

        for path, entry in list(self._files.items()):
            try:
                lines.extend((path, line, entry["parser"]) for line in self._read(path, entry))
            except OSError:
                # Dosya döndürme anında yok olabilir; sonraki yoklama/tarama düzeltir.
                continue

        return lines

    def _read(self, path, entry):
        info = os.stat(path)

        if info.st_ino != entry["inode"]:
            entry["inode"] = info.st_ino
            entry["offset"] = 0
        elif info.st_size < entry["offset"]:
            entry["offset"] = 0

        if info.st_size == entry["offset"]:
            return []

        with open(path, "rb") as handle:
            handle.seek(entry["offset"])
            chunk = handle.read(min(info.st_size - entry["offset"], MAX_READ))

        end = chunk.rfind(b"\n")

        if end < 0:
            if len(chunk) < MAX_LINE:
                return []  # yarım satır; tamamlanmasını bekle
            end = len(chunk) - 1
            chunk = chunk + b"\n"

        entry["offset"] += end + 1

        return [
            raw[:MAX_LINE].decode("utf-8", errors="replace").rstrip("\r")
            for raw in chunk[: end + 1].split(b"\n")[:-1]
            if raw.strip()
        ]

    # ------------------------------------------------------------------ #

    def files(self):
        return sorted(self._files)

    def state(self):
        """Kaydedilecek konumlar."""
        return {path: {"inode": entry["inode"], "offset": entry["offset"]} for path, entry in self._files.items()}
