"""vitalwatch — VitalWatch Hub için Python raporlayıcı.

Kapsamı bilinçli olarak dar: **sunucu tarafı.** Tarayıcı hataları hub'dan
servis edilen ``t.js`` ile toplanır; o dosya pakete taşınmaz — hosted olması
bir düzeltmenin dakikalar içinde tüm sitelere yayılmasını sağlıyor, paket
olsaydı her düzeltme için bütün projelerin yeniden dağıtılması gerekirdi.

Tipik kurulum (Flask)::

    from vitalwatch.flask import VitalWatchFlask

    VitalWatchFlask(app)

Tipik kurulum (çerçevesiz)::

    import vitalwatch

    vitalwatch.hook_process()
    ...
    vitalwatch.report(error)
"""

import atexit
import os
import sys
import threading

from . import scrubber
from .env import env as read_env
from .release import detect as detect_release_label
from .reporter import SDK_VERSION, Reporter

__version__ = SDK_VERSION

#: Canlılık isteklerinin aralığı (saniye).
#:
#: Hub 24 saat ses çıkmayan kurulumu "sessiz" sayıyor. Aralık eşiğe eşit
#: olsaydı tek bir kaçırılan istek — deploy, yeniden başlatma, birkaç dakikalık
#: ağ kesintisi — projeyi anında bozuk gösterirdi. Üçte bir güvenli tolerans:
#: iki tur kaçsa bile alarm çalmaz.
HEARTBEAT_SECONDS = 8 * 60 * 60

_instance = None
_heartbeat = None
_heartbeat_stop = None
#: hook_process süreç başına bir kez kurulur; her çağrı excepthook'u bir kat
#: daha zincirliyordu (VitalWatchFlask iki kez, her ASGI yığını kurulumu).
_hooks_installed = False
# RLock: start_heartbeat kilidi tutarken reporter() çağırıyor ve düz bir Lock
# burada kendi kendine kilitleniyordu.
_lock = threading.RLock()


def from_environment(detect_release=True):
    """Ortam değişkenlerinden yapılandırma.

    İsimler kardeş paketlerle (Laravel, Node) birebir aynı: aynı projeyi iki
    dilde izleyen ekip iki ayrı isim seti öğrenmek zorunda kalmasın.

    ``release`` boşsa kendiliğinden bulunur (bkz. ``vitalwatch.release``):
    ``VITALWATCH_RELEASE`` → CI/PaaS commit değişkenleri → çalışma dizinindeki
    ``.git``. Kurulumda bir kez çözülür, olay başına değil.
    """
    values = read_env()
    release = detect_release_label(values=values)[0] if detect_release else None

    return {
        "enabled": values.get("VITALWATCH_ENABLED") != "false",
        "url": values.get("VITALWATCH_URL"),
        "key": values.get("VITALWATCH_KEY"),
        "secret": values.get("VITALWATCH_SECRET"),
        "env": values.get("VITALWATCH_ENV") or os.environ.get("APP_ENV") or "production",
        "release": release,
        "timeout": _number(values.get("VITALWATCH_TIMEOUT"), 2.0),
        "slow_request_ms": _number(values.get("VITALWATCH_SLOW_REQUEST_MS"), 1000),
    }


def init(**options):
    """Raporlayıcıyı kurar.

    Çağrılmazsa ilk kullanımda ortam değişkenlerinden kendiliğinden kurulur —
    kurulum adımını unutmak sessiz arıza üretmesin.
    """
    global _instance

    # Açıkça verilen release kazanır; o durumda .git okunmaz.
    settings = from_environment(detect_release=options.get("release") is None)
    settings.update({key: value for key, value in options.items() if value is not None})

    _instance = Reporter(**settings)

    # Gunicorn --preload ile fork ediyor; çocuk süreçte gönderim thread'i
    # yoktur ve temizlenmezse olaylar ölü bir kuyruğa yazılır.
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_instance._after_fork)

    # Kancalar init'ten önce kurulduysa (VitalWatchFlask(app) sonra vitalwatch.init(...))
    # canlılık o an yapılandırma eksik diye başlamamıştı; burada yeniden denenir.
    if _hooks_installed:
        try:
            start_heartbeat()
        except Exception:  # noqa: BLE001
            pass

    return _instance


def reporter():
    global _instance

    with _lock:
        if _instance is None:
            init()

    return _instance


def report(error, kind=None, route=None, method=None, block=False):
    """Bir hatayı hub'a bildirir. Hiçbir koşulda hata fırlatmaz."""
    try:
        return reporter().record_exception(
            error, kind=kind, route=route, method=method, block=block
        )
    except Exception as failure:  # noqa: BLE001
        # reporter() kurulumda patlayabilir (bozuk .env vb.); söz "fırlatmaz".
        return {"sent": False, "status": None, "error": str(failure)}


def hook_process():
    """Yakalanmamış istisnaları dinler ve canlılığı başlatır.

    Davranış **değiştirilmez**: mevcut hook zincirlenerek çağrılır, süreç
    sonlandırılmaz, çıkış kodu değiştirilmez. Bir izleme paketinin süreç
    yönetimine karışması, çözdüğü sorundan büyük bir sorundur.

    Süreç başına **bir kez** kurulur; tekrar çağrı yalnızca canlılığı yeniden
    dener. Kancalar raporlayıcıyı çağrı anında çözer: kurulumdan sonra
    ``vitalwatch.init(...)`` çağrılırsa olaylar yeni örneğe gider.
    """
    global _hooks_installed

    with _lock:
        if not _hooks_installed:
            _hooks_installed = True
            _install_hooks()

    start_heartbeat()

    return reporter()


def _install_hooks():
    previous_hook = sys.excepthook

    def excepthook(exc_type, value, tb):
        try:
            current = reporter()
            current.record_exception(value)
            # Süreç kapanıyor: daemon thread'in gönderimi bitirmesi beklenir,
            # yoksa son ve en önemli olay kaybolur.
            current.flush(current.client.timeout + 1)
        except Exception:  # noqa: BLE001
            pass

        previous_hook(exc_type, value, tb)

    sys.excepthook = excepthook

    # Thread içinde patlayan hata ana thread'in excepthook'una düşmez ve
    # kimse dinlemiyorsa hiçbir iz bırakmaz. Lens'te iş takibi thread'lerle
    # yürüdüğü için bu kanca kritik.
    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def thread_excepthook(args):
            try:
                if args.exc_value is not None:
                    reporter().record_exception(args.exc_value)
            except Exception:  # noqa: BLE001
                pass

            previous_thread_hook(args)

        threading.excepthook = thread_excepthook

    # Kapanışta kuyrukta kalanlar gönderilir; daemon thread aksi halde
    # yarıda kesilir.
    atexit.register(_flush_at_exit)


def _flush_at_exit():
    try:
        current = _instance

        # Hiç kurulmadıysa kapanışta raporlayıcı yaratılmaz.
        if current is not None:
            current.flush(current.client.timeout + 1)
    except Exception:  # noqa: BLE001
        pass


def start_heartbeat(interval=HEARTBEAT_SECONDS):
    """Süreç boyunca düzenli "buradayım" gönderir.

    Süreç başlarken hemen bir istek atılır: deploy sonrası kurulum kendini
    anında kanıtlar, sekiz saat beklemez.
    """
    global _heartbeat, _heartbeat_stop

    with _lock:
        if _heartbeat is not None:
            return _heartbeat

        current = reporter()

        if not current.configured():
            # Yapılandırma yoksa zamanlayıcı kurmanın anlamı yok.
            return None

        # Durdurma bayrağı her başlatmada yenilenir: modül düzeyinde tek bir
        # Event tutulsaydı bir kez durdurulan zamanlayıcı bir daha çalışmazdı.
        stop = threading.Event()
        _heartbeat_stop = stop

        def beat():
            while not stop.wait(interval):
                try:
                    # Çağrı anında çözülür: sonradan init edilen örneğe gider.
                    reporter().heartbeat(block=True)
                except Exception:  # noqa: BLE001
                    pass

        # İlk istek kuyruktan gider ki kurulum anında bloke olmasın.
        current.heartbeat(block=False)

        # daemon: kısa ömürlü bir betik işini bitirince kapanmalı; izleme
        # zamanlayıcısı onu sekiz saat ayakta tutarsa CLI komutları dönmez.
        _heartbeat = threading.Thread(target=beat, name="heartbeat", daemon=True)
        _heartbeat.start()

    return _heartbeat


def stop_heartbeat():
    global _heartbeat, _heartbeat_stop

    if _heartbeat_stop is not None:
        _heartbeat_stop.set()

    _heartbeat = None
    _heartbeat_stop = None


def _number(value, fallback):
    try:
        return type(fallback)(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "Reporter",
    "SDK_VERSION",
    "__version__",
    "from_environment",
    "hook_process",
    "init",
    "report",
    "reporter",
    "scrubber",
    "start_heartbeat",
    "stop_heartbeat",
]
