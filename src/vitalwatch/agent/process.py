"""Satır akıtan alt süreç (``journalctl -f``, ``docker events``, ``docker logs -f``).

Her alt süreç kendi thread'inde okunur: biri beklerken diğerleri durmaz.
Komut yoksa (``FileNotFoundError``) kaynak bir kez uyarı yazıp kapanır; komut
izin hatasıyla ya da başka bir sebeple çıkarsa uyarı **bir kez** yazılır ve
üstel bekleme ile yeniden denenir — Docker sonradan kurulabilir, servis
yeniden başlayabilir. Ajan hiçbir durumda bu yüzden durmaz.
"""

import subprocess
import sys
import threading
import time

#: İlk yeniden başlatma beklemesi ve üst sınırı (saniye).
RESTART_DELAY = 10.0
MAX_RESTART_DELAY = 300.0

#: Bu kadar süre çalışan süreç "sağlıklı" sayılır; bekleme sıfırlanır.
HEALTHY_SECONDS = 60.0


def warn(message):
    print(f"vitalwatch-agent: {message}", file=sys.stderr, flush=True)


class LineProcess(threading.Thread):
    def __init__(self, name, command, on_line, stop_event, warn_func=None, popen=subprocess.Popen):
        """:param command: liste ya da liste döndüren fonksiyon (imleç gibi
        her başlatmada değişen argümanlar için)."""
        super().__init__(name=f"vitalwatch-agent-{name}", daemon=True)
        self.source_name = name
        self._command = command
        self._on_line = on_line
        self._stop_event = stop_event
        self._warn = warn_func or warn
        self._popen = popen
        self._process = None
        self._warned = set()
        self.available = True

    def _warn_once(self, message):
        if message not in self._warned:
            self._warned.add(message)
            self._warn(message)

    def run(self):
        delay = RESTART_DELAY

        while not self._stop_event.is_set():
            command = self._command() if callable(self._command) else list(self._command)

            try:
                process = self._popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                self.available = False
                self._warn_once(f"{command[0]} not found; {self.source_name} source disabled.")
                return
            except OSError as error:
                self.available = False
                self._warn_once(f"{command[0]} could not be started ({error}); {self.source_name} source disabled.")
                return

            self._process = process
            errors = []
            reader = threading.Thread(target=self._drain, args=(process.stderr, errors), daemon=True)
            reader.start()
            started = time.monotonic()

            try:
                for raw in iter(process.stdout.readline, b""):
                    try:
                        self._on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                    except Exception as error:  # noqa: BLE001 — tek satır ajanı düşürmesin
                        self._warn_once(f"could not process {self.source_name} line: {error}")
            except (OSError, ValueError):
                pass

            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait()

            reader.join(timeout=1)

            if self._stop_event.is_set():
                return

            detail = " ".join(errors).strip()[:300] or f"exit code {code}"
            self._warn_once(f"{self.source_name} source stopped: {detail} — will retry.")

            if time.monotonic() - started >= HEALTHY_SECONDS:
                delay = RESTART_DELAY

            self._stop_event.wait(delay)
            delay = min(delay * 2, MAX_RESTART_DELAY)

    @staticmethod
    def _drain(stream, errors):
        try:
            for raw in iter(stream.readline, b""):
                if len(errors) < 5:
                    errors.append(raw.decode("utf-8", errors="replace").strip())
        except (OSError, ValueError):
            pass

    def stop(self):
        process = self._process

        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
