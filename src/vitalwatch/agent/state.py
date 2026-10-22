"""Okuma konumları ve imleçler: ``/var/lib/vitalwatch-agent/state.json``.

Yazım atomiktir (geçici dosya + ``os.replace``): servis tam yazarken
öldürülürse yarım bir JSON kalmaz, bir sonraki açılışta tüm loglar baştan
okunmaz. Varsayılan dizin yazılamıyorsa (root olmadan elle çalıştırma)
kullanıcının durum dizinine düşülür — ajan yine de çalışır.
"""

import json
import os
import sys
import tempfile

DEFAULT_DIR = "/var/lib/vitalwatch-agent"
FILE_NAME = "state.json"


def _fallback_dirs():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")

    return [os.path.join(base, "vitalwatch-agent"), os.path.join(tempfile.gettempdir(), "vitalwatch-agent")]


def _writable_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return False

    return os.access(path, os.W_OK | os.X_OK)


class StateStore:
    def __init__(self, directory=None):
        """``directory`` verilmezse ``VITALWATCH_AGENT_STATE_DIR`` ya da varsayılan."""
        preferred = directory or os.environ.get("VITALWATCH_AGENT_STATE_DIR") or DEFAULT_DIR
        self.directory = None

        for candidate in [preferred] + _fallback_dirs():
            if _writable_dir(candidate):
                self.directory = candidate
                break

        if self.directory != preferred:
            print(
                f"vitalwatch-agent: {preferred} is not writable; state file "
                + (f"kept under {self.directory}." if self.directory else "cannot be kept."),
                file=sys.stderr,
            )

    @property
    def path(self):
        return os.path.join(self.directory, FILE_NAME) if self.directory else None

    def load(self):
        """Durum sözlüğü; dosya yoksa ya da bozuksa boş."""
        if not self.path:
            return {}

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}

        return data if isinstance(data, dict) else {}

    def save(self, data):
        """Atomik yazım. Başarısızlık ajanı durdurmaz; ``False`` döner."""
        if not self.path:
            return False

        temp_path = None

        try:
            descriptor, temp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.directory)

            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())

            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)

            return True
        except OSError:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

            return False
