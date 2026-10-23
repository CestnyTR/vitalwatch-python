"""vitalwatch — Python reporter for VitalWatch Hub.

The scope is deliberately narrow: **server side only.** Browser errors are
collected by ``t.js``, served by the hub; that file is not shipped in this
package — because it is hosted, a fix reaches every site within minutes,
whereas a packaged copy would require redeploying every project.

Typical setup (Flask)::

    from vitalwatch.flask import VitalWatchFlask

    VitalWatchFlask(app)

Typical setup (no framework)::

    import vitalwatch

    vitalwatch.hook_process()
    ...
    vitalwatch.report(error)
"""

import atexit
import os
import sys
import threading
import time

from . import dependencies, scrubber
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
    """Build the configuration from environment variables.

    The names are identical to the sibling packages (Laravel, Node), so a team
    monitoring the same project in two languages learns one set of names.

    If ``release`` is empty it is detected automatically (see
    ``vitalwatch.release``): ``VITALWATCH_RELEASE`` → CI/PaaS commit variables →
    ``.git`` in the working directory. It is resolved once at setup, not per event.
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
        "dependencies": dependencies.enabled_in(values),
    }


def init(**options):
    """Set up the reporter.

    If it is never called, the reporter is set up from environment variables
    on first use — forgetting the setup step must not cause a silent failure.
    Keyword arguments override the environment.
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
    """Return the active reporter, setting it up from the environment if needed."""
    global _instance

    with _lock:
        if _instance is None:
            init()

    return _instance


def report(error, kind=None, route=None, method=None, block=False):
    """Report an exception to the hub. Never raises."""
    try:
        return reporter().record_exception(
            error, kind=kind, route=route, method=method, block=block
        )
    except Exception as failure:  # noqa: BLE001
        # reporter() kurulumda patlayabilir (bozuk .env vb.); söz "fırlatmaz".
        return {"sent": False, "status": None, "error": str(failure)}


def hook_process():
    """Listen for uncaught exceptions and start the heartbeat.

    Behaviour is **not changed**: the existing hook is chained and still
    called, the process is not terminated and the exit code is not altered.
    A monitoring package interfering with process management would be a
    bigger problem than the one it solves.

    Installed **once** per process; calling it again only retries the
    heartbeat. The hooks resolve the reporter at call time: if
    ``vitalwatch.init(...)`` is called afterwards, events go to the new instance.
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
    """Send a periodic "I am alive" request for the lifetime of the process.

    The first request is sent immediately at startup, so a fresh deploy proves
    itself right away instead of after eight hours.

    The same background thread also sends the installed dependency list
    (``vitalwatch.dependencies``): about 60 seconds after start, then every
    24 hours and whenever the list changes. ``VITALWATCH_DEPENDENCIES=false``
    turns that off.

    Only the process that started the thread sends. After a fork (gunicorn
    ``--preload``) the child inherits a stopped thread and does not start a
    new one, so N workers do not mean N heartbeats or N dependency lists.
    """
    global _heartbeat, _heartbeat_stop

    with _lock:
        if _heartbeat is not None:
            # Fork sonrası çocukta bu thread ölüdür ve bilerek yeniden
            # başlatılmaz: gunicorn --preload'da ana süreç (arbiter) yaşamaya
            # devam eder ve canlılığı + bağımlılık listesini o gönderir.
            # Çocuklarda yeniden başlatmak N işçi × istek demek olurdu.
            return _heartbeat

        current = reporter()

        if not current.configured():
            # Yapılandırma yoksa zamanlayıcı kurmanın anlamı yok.
            return None

        # Durdurma bayrağı her başlatmada yenilenir: modül düzeyinde tek bir
        # Event tutulsaydı bir kez durdurulan zamanlayıcı bir daha çalışmazdı.
        stop = threading.Event()
        _heartbeat_stop = stop
        schedule = dependencies.Schedule(time.monotonic())

        # İlk istek kuyruktan gider ki kurulum anında bloke olmasın.
        current.heartbeat(block=False)

        # daemon: kısa ömürlü bir betik işini bitirince kapanmalı; izleme
        # zamanlayıcısı onu sekiz saat ayakta tutarsa CLI komutları dönmez.
        _heartbeat = threading.Thread(
            target=_timer_loop, args=(stop, interval, schedule), name="heartbeat", daemon=True
        )
        _heartbeat.start()

    return _heartbeat


def _timer_loop(stop, interval, schedule, clock=time.monotonic):
    """Canlılık thread'inin döngüsü: canlılık + bağımlılık zamanlaması.

    Saat ve durdurma bayrağı parametre: testler sahte saatle (``stop.wait``
    saati ilerletir) 24 saati milisaniyede koşturur. Raporlayıcı her turda
    çağrı anında çözülür — sonradan init edilen örneğe gider.
    """
    next_beat = clock() + interval

    while True:
        try:
            deadline = min(next_beat, schedule.next_check) if schedule is not None else next_beat
            wait = max(deadline - clock(), 0)
        except Exception:  # noqa: BLE001
            wait = interval

        if stop.wait(wait):
            return

        now = clock()

        if now >= next_beat:
            try:
                reporter().heartbeat(block=True)
            except Exception:  # noqa: BLE001
                pass

            next_beat = clock() + interval

        if schedule is not None and schedule.due(now):
            try:
                schedule.run(now, reporter())
            except Exception:  # noqa: BLE001
                # run() zaten fırlatmaz; reporter() kurulumda patlarsa bile
                # thread ölmesin ve meşgul döngüye girmesin.
                schedule.next_check = now + schedule.check_every


def stop_heartbeat():
    """Stop the heartbeat started by ``start_heartbeat``."""
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
    "dependencies",
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
