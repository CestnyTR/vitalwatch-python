"""Installed dependency list for the hub's vulnerability advisories.

The hub checks the list against OSV.dev and alerts per project. The source is
the **running interpreter's installed distributions** (``importlib.metadata``),
not a lock file: it is what actually runs, including transitive packages and
anything installed by hand on the server.

The list is sent from the heartbeat thread — first about 60 seconds after
start, then every 24 hours, and again whenever its hash changes. It never runs
on the request path and never raises.
"""

import hashlib
import re
import sys

from .client import DEPENDENCIES_MAX_BODY_BYTES

#: Gövde biçim sürümü ve kardeş SDK'larla ortak sabitler (sözleşme).
FORMAT_VERSION = 1
ECOSYSTEM = "PyPI"
MANIFEST = "installed-distributions"

#: Paket sayısı sınırı; aşılırsa sıralı listenin başı gönderilir.
MAX_PACKAGES = 5000

#: Gövde sınırı (tek kaynak client'ta). Hub 512 KB'a kadar kabul ediyor; pay
#: bırakmak için 500 KB. Aşarsa hiçbir şey gönderilmez.
MAX_BODY_BYTES = DEPENDENCIES_MAX_BODY_BYTES

#: Zamanlama (saniye, monotonic saat).
#: Açılıştan sonra kısa bir bekleme: kısa ömürlü betikler (CLI, cron, migrate)
#: hiç göndermesin, yalnızca ayakta kalan süreçler göndersin.
STARTUP_DELAY_SECONDS = 60
#: Liste değişmese de günde bir kez yeniden gönderilir (hub tarafında tazelik).
RESEND_SECONDS = 24 * 60 * 60
#: Hash değişimi için yeniden tarama aralığı. Tarama yerel (dosya okuma), ağa
#: çıkmaz; çoğu zaman hash aynıdır ve hiçbir şey gönderilmez. Ağ hatasında
#: bir sonraki taramada yeniden denenir.
CHECK_SECONDS = 60 * 60

#: Gönderilmeyen dağıtımlar (normalize edilmiş adlarla):
#: - pip, setuptools, wheel: paketleme araç zinciri. Her sanal ortamda bulunur,
#:   çoğu zaman ensurepip'in getirdiği eski bir sürümle; açıkları kurulum
#:   anına aittir, uygulama çalışırken import edilmez. Göndermek her projede
#:   aynı, uygulamayla ilgisiz bir uyarı üretir — uyarı yorgunluğu, asıl
#:   uyarının da görmezden gelinmesine yol açar.
#: - pkg-resources: Debian/Ubuntu venv'lerindeki sahte ``pkg_resources==0.0.0``.
#: - vitalwatch: sürümü zaten her istekte ``sdk_version`` olarak gidiyor.
EXCLUDED = frozenset({"pip", "setuptools", "wheel", "pkg-resources", "vitalwatch"})

_NAME_SEPARATORS = re.compile(r"[-_.]+")

#: Büyük gövde uyarısı süreç başına bir kez yazılır.
_warned_too_large = False

#: VITALWATCH_DEPENDENCIES için kapalı sayılan değerler (agent.ini ile aynı küme).
_OFF = ("off", "no", "false", "0", "disabled", "hayir", "hayır", "kapali", "kapalı")


def normalize_name(name):
    """PEP 503 normalized name: lowercase, runs of ``-``, ``_`` and ``.`` → ``-``."""
    return _NAME_SEPARATORS.sub("-", str(name).strip()).lower()


def enabled_in(values):
    """``VITALWATCH_DEPENDENCIES`` flag from a config dict; on unless explicitly off."""
    value = (values or {}).get("VITALWATCH_DEPENDENCIES")

    if value is None:
        return True

    return str(value).strip().lower() not in _OFF


def collect(distributions=None):
    """Installed distributions as ``[{"name": ..., "version": ...}]``.

    Names are PEP 503 normalized, duplicates removed (the first one on
    ``sys.path`` wins — that is the one Python imports), excluded tooling
    dropped, sorted by name then version and capped at ``MAX_PACKAGES``.
    Never raises; returns an empty list if metadata cannot be read.
    """
    try:
        if distributions is None:
            from importlib import metadata

            distributions = metadata.distributions()

        seen = {}

        for distribution in distributions:
            # Bozuk bir METADATA tüm listeyi düşürmesin; o dağıtım atlanır.
            # ``distribution.version`` yerine .get: Version alanı olmayan bir
            # dağıtımda 3.12+ DeprecationWarning basıyor (ileride KeyError).
            try:
                metadata = distribution.metadata
                raw_name = metadata.get("Name")
                version = metadata.get("Version")
            except Exception:  # noqa: BLE001
                continue

            if not raw_name or not version:
                continue

            name = normalize_name(raw_name)
            version = str(version).strip()

            if not name or not version or name in EXCLUDED or name in seen:
                continue

            seen[name] = version

        packages = sorted(seen.items())[:MAX_PACKAGES]

        return [{"name": name, "version": version} for name, version in packages]
    except Exception:  # noqa: BLE001
        return []


def canonical_hash(packages):
    """SHA-256 hex of the canonical list: sorted, one ``name@version\\n`` per package."""
    lines = sorted((item["name"], item["version"]) for item in packages)
    text = "".join(f"{name}@{version}\n" for name, version in lines)

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest(packages):
    """The ``dependencies`` object of the request body."""
    return {
        "v": FORMAT_VERSION,
        "ecosystem": ECOSYSTEM,
        "manifest": MANIFEST,
        "hash": canonical_hash(packages),
        "packages": list(packages),
    }


def warn_too_large(size):
    """Gövde sınırı aşıldı; süreç başına bir kez stderr'e yazar."""
    global _warned_too_large

    if _warned_too_large:
        return

    _warned_too_large = True

    try:
        print(
            f"vitalwatch: the dependency list is {size} bytes, over the {MAX_BODY_BYTES} byte limit; "
            "it was not sent. Set VITALWATCH_DEPENDENCIES=false to silence this.",
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # noqa: BLE001
        pass


class Schedule:
    """Bağımlılık gönderiminin zamanlaması. Saat dışarıdan verilir (test edilebilir).

    - ilk tarama ``started_at + STARTUP_DELAY_SECONDS``;
    - sonra her ``CHECK_SECONDS``'ta bir yerel tarama;
    - gönderim: hiç gönderilmediyse, hash değiştiyse ya da son başarılı
      gönderimden ``RESEND_SECONDS`` geçtiyse.
    """

    def __init__(
        self,
        started_at,
        startup_delay=STARTUP_DELAY_SECONDS,
        resend=RESEND_SECONDS,
        check_every=CHECK_SECONDS,
    ):
        self.resend = resend
        self.check_every = check_every
        self.next_check = started_at + startup_delay
        self.last_sent_at = None
        self.last_hash = None

    def due(self, now):
        return now >= self.next_check

    def should_send(self, now, digest):
        if self.last_sent_at is None or digest != self.last_hash:
            return True

        return now - self.last_sent_at >= self.resend

    def run(self, now, reporter, collect_packages=None):
        """Bir tarama turu. Gönderildiyse sonucu, gönderilmediyse ``None`` döner.

        Hiç fırlatmaz; ne olursa olsun bir sonraki tarama zamanı ilerletilir —
        aksi halde bozuk bir durumda thread meşgul döngüye girerdi.
        """
        self.next_check = now + self.check_every

        try:
            if not reporter.wants_dependencies():
                return None

            packages = (collect_packages or collect)()
            digest = canonical_hash(packages)

            if not self.should_send(now, digest):
                return None

            result = reporter.send_dependencies(packages=packages)

            # Büyük gövde de "işlendi" sayılır: aynı liste her saat yeniden
            # denenmez. Ağ hatası sayılmaz — bir sonraki taramada yeniden gider.
            if result.get("sent") or result.get("error") == "body-too-large":
                self.last_sent_at = now
                self.last_hash = digest

            return result
        except Exception:  # noqa: BLE001
            return None
