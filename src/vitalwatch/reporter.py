"""Olayları biriktirir ve hub'a gönderir.

Node paketinden yapısal fark **gönderimin nasıl beklenmediğidir.** Node'da
``fetch`` zaten beklenmeyen bir promise; burada WSGI senkron çalışır ve
``send()`` çağrısı isteği doğrudan geciktirirdi. Bu yüzden olaylar sınırlı bir
kuyruğa bırakılır, tek bir arka plan thread'i gönderir (davranış garantisi 4).

Kuyruk **sınırlı ve dolduğunda olay düşürülür**: bir hata fırtınasında
sınırsız büyüyen kuyruk, izlenen uygulamanın belleğini yer — izleme paketinin
izlediği uygulamayı öldürmesi, çözdüğü sorundan büyük bir sorundur.
"""

import atexit
import os
import queue
import sys
import threading
import traceback

from . import scrubber
from .client import HubClient

SDK_VERSION = "0.2.0"

#: Kuyruk derinliği. Hata fırtınasında bellek sınırı; hub'ın istek başına
#: kabul ettiği 8 olayın epey üstünde, normal işleyişte hiç dolmaz.
QUEUE_SIZE = 100

#: Hub'ın kabul ettiği sunucu türleri (vitalwatch-hub, IngestService::KINDS_SERVER).
#: Küme dışı bir ``kind`` sessizce reddedilir, o yüzden burada da doğrulanır.
KINDS = (
    "exception",
    "fatal",
    "http_5xx",
    "slow_request",
    "slow_query",
    "job_failed",
    "command_failed",
)

#: Hub'ın kabul ettiği ortamlar. Küme dışıysa olay atılır.
ENVIRONMENTS = ("production", "staging", "local")

#: Ortam takma adları — Node ve Laravel paketleriyle aynı tablo.
_ENV_ALIASES = {
    "production": "production", "prod": "production", "live": "production",
    "staging": "staging", "stage": "staging", "stg": "staging", "preprod": "staging", "uat": "staging",
    "local": "local", "dev": "local", "development": "local", "test": "local", "testing": "local",
}


def normalize_env(value):
    """Ortam adını hub'ın kabul ettiği karşılığa çevirir.

    Hub yalnızca production/staging/local kabul ediyor. Eskiden tanınmayan
    değer sessizce "production" sayılıyordu: ``APP_ENV=testing`` ile çalışan
    bir kurulumun verisi canlı verinin arasına karışıyordu. Artık takma adlar
    çevrilir; tanınmayan değer olduğu gibi gider (hub onu panelde "ortam"
    reddi olarak sayar) ve ``vitalwatch-status`` hata verir.
    """
    if value is None:
        return "production"

    text = str(value).strip()

    if not text:
        return "production"

    return _ENV_ALIASES.get(text.lower(), text)


class Reporter:
    def __init__(
        self,
        url=None,
        key=None,
        secret=None,
        enabled=True,
        env="production",
        release=None,
        source="server",
        timeout=2.0,
        slow_request_ms=1000,
        ignore=(),
    ):
        self.client = HubClient(url=url, key=key, secret=secret, timeout=timeout)
        self.enabled = enabled
        self.env = normalize_env(env)
        self.release = release
        self.source = "ssr" if source == "ssr" else "server"
        self.slow_request_ms = slow_request_ms
        self.ignore = tuple(ignore or ())

        # Tekilleştirme için ayrı bir koleksiyon YOK; işaret istisnanın
        # üzerine yazılır (bkz. _mark). Node paketi burada WeakSet kullanıyor
        # ama Python'da yerleşik istisnalar weakref kabul etmez —
        # weakref.ref(ValueError()) TypeError fırlatır. Aynı yapıyı kopyalamak
        # sessizce hiç çalışmayan bir tekilleştirme demek olurdu.

        self._queue = None
        self._worker = None
        self._lock = threading.Lock()
        self._dropped = 0

    # ------------------------------------------------------------------ #
    # Kayıt
    # ------------------------------------------------------------------ #

    def configured(self):
        return self.enabled and self.client.configured()

    def record_exception(self, error, kind=None, route=None, method=None, block=False):
        """Bir istisnayı raporlar.

        Hiçbir koşulda hata fırlatmaz ve beklenmesi gerekmez: ``block=False``
        iken kuyruğa bırakılıp hemen döner.
        """
        try:
            if not self.configured():
                return {"sent": False, "status": None, "error": "missing-config"}

            if self._ignored(error):
                return {"sent": False, "status": None, "error": "ignored"}

            if not self._mark(error):
                return {"sent": False, "status": None, "error": "already-reported"}

            message = str(error) or error.__class__.__name__

            return self.send(
                {
                    "kind": kind or self._kind_for(error),
                    # Veritabanı hataları SQL'i bağlanmış değerlerle taşır;
                    # oturum kimliği, e-posta, kart numarası oradan sızabilir.
                    "msg": scrubber.message(message, self._contains_sql(error)),
                    "exception_class": error.__class__.__name__,
                    "stack": scrubber.stack(self._traceback(error)),
                    "route": route,
                    "method": method,
                },
                block=block,
            )
        except Exception as error:  # noqa: BLE001
            # Kendi hatasını raporlamaz — sonsuz döngü riski.
            return {"sent": False, "status": None, "error": str(error)}

    def record_request(self, route, method, status, duration_ms, block=False):
        """İstek bitiminde çağrılır.

        Yalnızca yavaş istek veya 5xx raporlanır; her isteği göndermek izlenen
        uygulamaya da hub'a da yük olurdu.
        """
        try:
            if not self.wants_request(status, duration_ms):
                return None

            label = f"{method} {route}"

            return self.send(
                {
                    "kind": "http_5xx" if status >= 500 else "slow_request",
                    "msg": (
                        f"{label} — HTTP {status}"
                        if status >= 500
                        else f"{label} — {round(duration_ms)} ms"
                    ),
                    "route": label,
                    "method": method,
                    "status": status,
                    "duration_ms": round(duration_ms),
                    # Node ve Laravel'le aynı alan; ölçülemezse gönderilmez.
                    "memory_mb": memory_mb(),
                },
                block=block,
            )
        except Exception:  # noqa: BLE001
            return None

    def wants_request(self, status, duration_ms):
        """Bu istek gönderilecek mi: yapılandırılmış ve (5xx ya da yavaş).

        Entegrasyonlar rota desenini yalnızca ``True`` ise hesaplar; her hızlı
        200 için yönlendirici taranmasın.
        """
        try:
            if not self.configured():
                return False

            return status >= 500 or duration_ms >= self.slow_request_ms
        except Exception:  # noqa: BLE001
            return False

    def heartbeat(self, block=True):
        """"Buradayım" — olay taşımayan canlılık isteği.

        Hub'ın bir kurulumun çalıştığını anlamasının tek yolu hata gelmesiydi;
        sonuç ters dönüyordu: hatasız çalışan uygulama "kurulum bozuk"
        görünüyordu. Artık kanıt isteğin kendisi — boş bir toplu istek yeterli.

        Varsayılan olarak **bekler**: canlılık isteği sıcak yolda değil,
        sonucunu okuyan tek yer de teşhis komutu.
        """
        if not self.configured():
            return {"sent": False, "status": None, "error": "missing-config"}

        return self.send({"events": []}, block=block)

    # ------------------------------------------------------------------ #
    # Gönderim
    # ------------------------------------------------------------------ #

    def send(self, event, block=False):
        """Ortak alanları ekleyip gönderir ya da kuyruğa bırakır."""
        body = dict(event)
        body.update(
            {
                "env": self.env,
                "source": self.source,
                "release": self.release,
                # Çalışma ortamı ve SDK sürümü. Hub bunları bağlamda saklıyor;
                # sdk_version olmadan hangi projenin eski sürümde kaldığı
                # görülemez ve güncelleme körlemesine yapılır.
                "runtime": "python",
                "runtime_version": sys.version.split()[0],
                "sdk_version": SDK_VERSION,
            }
        )

        body = {key: value for key, value in body.items() if value is not None}

        if block:
            return self.client.send(body)

        return self._enqueue(body)

    def _enqueue(self, body):
        """Kuyruğa bırak. Kuyruk doluysa olayı düşür — asla bloke etme."""
        try:
            self._ensure_worker().put_nowait(body)

            return {"sent": None, "status": None, "error": None, "queued": True}
        except queue.Full:
            self._dropped += 1

            return {"sent": False, "status": None, "error": "queue-full"}

    def _ensure_worker(self):
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._queue = queue.Queue(maxsize=QUEUE_SIZE)
                # daemon: kısa ömürlü bir betik işini bitirince kapanmalı,
                # izleme thread'i onu ayakta tutmamalı. Kapanışta kaybolan
                # olaylar için atexit'te kısa bir bekleme var.
                self._worker = threading.Thread(
                    target=self._drain, name="vitalwatch-sender", daemon=True
                )
                self._worker.start()

            return self._queue

    def _drain(self):
        while True:
            body = self._queue.get()

            try:
                self.client.send(body)
            except Exception:  # noqa: BLE001
                # HubClient zaten yutuyor; bu ikinci ağ yalnızca thread'in
                # ölmemesi için — ölürse sonraki olaylar sessizce birikir.
                pass
            finally:
                self._queue.task_done()

    def flush(self, timeout=3.0):
        """Kuyruğu boşaltmayı bekler. Kapanışta ve testlerde çağrılır."""
        current = self._queue

        if current is None:
            return True

        deadline = threading.Event()
        waiter = threading.Thread(target=lambda: (current.join(), deadline.set()), daemon=True)
        waiter.start()

        return deadline.wait(timeout)

    def _after_fork(self):
        """Fork sonrası çocukta thread yoktur; kuyruk yeniden kurulmalı.

        Gunicorn ``--preload`` ile ana süreçte import edip fork ediyor. Bu
        temizlik olmadan çocuk süreç ölü bir thread'e olay yazar ve hiçbir şey
        gönderilmez — tam da bu ailenin tekrarlayan sessiz arızası.
        """
        self._queue = None
        self._worker = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Yardımcılar
    # ------------------------------------------------------------------ #

    @staticmethod
    def _kind_for(error):
        """Türü seç.

        ``fatal`` ayrı bir tür: yorumlayıcının sınırına çarpmak, uygulamanın
        yakalayabildiği bir hatadan başka bir şeydir ve panelde ayrı süzülmesi
        gerekir.
        """
        if isinstance(error, (MemoryError, RecursionError, SystemError)):
            return "fatal"

        return "exception"

    @staticmethod
    def _traceback(error):
        tb = getattr(error, "__traceback__", None)

        if tb is None:
            return None

        return "".join(traceback.format_exception(type(error), error, tb))

    @staticmethod
    def _mark(error):
        """Daha önce raporlandıysa ``False``.

        İşaret istisnanın kendi üzerinde taşınır: ayrı bir koleksiyon tutmak
        uzun ömürlü süreçte ya bellek sızdırır ya da id yeniden kullanımı
        yüzünden yanlış eşleşir. Nesne çöp toplandığında işaret de gider.

        ``__slots__`` tanımlayan bir istisna sınıfında yazma başarısız olur;
        o durumda tekilleştirme yapılmaz ve olay gönderilir — iki kayıt, hiç
        kayıt olmamasından iyidir.
        """
        if getattr(error, "_vitalwatch_reported", False):
            return False

        try:
            error._vitalwatch_reported = True
        except (AttributeError, TypeError):
            pass

        return True

    @staticmethod
    def _contains_sql(error):
        """Mesajı SQL taşıyan hatalar.

        Sürücü paketlerine bağımlılık kurulmadığı için ada ve içeriğe bakılır.
        """
        name = f"{error.__class__.__module__}.{error.__class__.__name__}".lower()
        message = str(error).lower()

        if any(word in name for word in ("sql", "psycopg", "mysql", "sqlite", "database", "operationalerror", "integrityerror")):
            return True

        return any(word in message for word in ("select ", "insert ", "update ", "delete "))

    def _ignored(self, error):
        """Yok sayılan hatalar. Sınıf ya da sınıf adı (metin) verilebilir."""
        for entry in self.ignore:
            if isinstance(entry, str):
                if entry == error.__class__.__name__:
                    return True
            elif isinstance(entry, type) and isinstance(error, entry):
                return True

        return False


def memory_mb():
    """Sürecin tepe bellek kullanımı (RSS), MB. Ölçülemezse ``None``.

    ``resource`` Windows'ta yok. ``ru_maxrss`` Linux'ta KB, macOS'ta bayt.
    """
    try:
        import resource
    except ImportError:
        return None

    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(peak, (int, float)) or peak <= 0:
        return None

    divisor = 1048576 if sys.platform == "darwin" else 1024

    return int(round(peak / divisor))
