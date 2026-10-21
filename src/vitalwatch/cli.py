"""Kurulum teşhisi — ``vitalwatch-status``.

Paketin en pahalı arıza biçimi için var: **sessiz çalışmama.** Yapılandırma
eksikken ya da secret yanlışken hiçbir şey patlamaz, hiçbir log düşmez — hub
geçersiz isteğe de 204 döner (saldırgana geri bildirim verilmez). Sonuç,
kimsenin fark etmediği bir izleme kurulumu.

Komut tek başına "çalışıyor" diyemez: hub geçerli ile geçersiz imzayı dışarıya
aynı yanıtla karşılar. Yaptığı iş, yerelde yanlış olan ne varsa göstermek ve
doğrulamanın hub panelinden yapılacağını söylemek.
"""

import json
import os
import re
import sys
import urllib.request

from . import __version__, init, report, reporter
from .env import env as read_env
from .release import detect as detect_release
from .reporter import ENVIRONMENTS, normalize_env

#: Paket PyPI'da değil; sürümler GitHub etiketlerinden okunur.
TAGS_URL = "https://api.github.com/repos/CestnyTR/vitalwatch-python/tags?per_page=100"
REPO_URL = "https://github.com/CestnyTR/vitalwatch-python.git"
UPDATE_TIMEOUT = 2.0

#: Kararlı sürüm: ``1.2.3`` ya da ``v1.2.3``; ön sürüm (``-rc1``) sayılmaz.
_STABLE = re.compile(r"[vV]?(\d+)\.(\d+)\.(\d+)")

#: Panelden gelen secret'in uzunluğu. En sık kurulum hatası eksik kopyalamadır
#: ve sonucu sessizce hiçbir şey göndermemektir.
SECRET_LENGTH = 64


def _line(label, value):
    print(f"  {label:<18} {value}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # Süreç ortamı VE .env birlikte okunur. Yalnızca os.environ'a bakan bir
    # teşhis, .env doğru doldurulmuş bir kurulumda "TANIMSIZ" der — teşhis
    # aracının yanlış teşhis koyması, hiç teşhis koymamaktan kötüdür.
    values = read_env()
    secret = values.get("VITALWATCH_SECRET", "")
    url = values.get("VITALWATCH_URL", "")

    print("")
    _line("SDK sürümü", __version__)
    _update_lines(values)
    _line("Etkin", "HAYIR (VITALWATCH_ENABLED=false)" if values.get("VITALWATCH_ENABLED") == "false" else "evet")
    _line("Hub adresi", url or "TANIMSIZ")
    _line("Proje anahtarı", values.get("VITALWATCH_KEY") or "TANIMSIZ")
    _line("Secret uzunluğu", f"{len(secret)} karakter" if secret else "TANIMSIZ")
    env_raw = values.get("VITALWATCH_ENV") or os.environ.get("APP_ENV") or "production"
    env_name = normalize_env(env_raw)
    _line("Environment", env_name if env_name == env_raw else f"{env_name} ({env_raw})")
    _line("Python", sys.version.split()[0])
    release, source = detect_release(values=values)
    _line("Sürüm etiketi", f"{release or 'tanımsız'} (kaynak: {source or 'yok'})")
    print("")

    problems = []

    if env_name not in ENVIRONMENTS:
        problems.append(
            f'Ortam "{env_raw}" hub tarafından kabul edilmez — olaylar ve canlılık sessizce '
            "reddedilir. VITALWATCH_ENV=production, staging ya da local yazın."
        )

    if values.get("VITALWATCH_ENABLED") == "false":
        problems.append("VITALWATCH_ENABLED=false — hiçbir veri gönderilmez.")

    missing = [key for key in ("VITALWATCH_URL", "VITALWATCH_KEY", "VITALWATCH_SECRET") if not values.get(key)]

    if missing:
        problems.append(f"{', '.join(missing)} tanımlı değil.")

    if secret and len(secret) != SECRET_LENGTH:
        problems.append(
            f"VITALWATCH_SECRET {SECRET_LENGTH} karakter olmalı, {len(secret)} karakter. "
            "Eksik kopyalanmış olabilir."
        )

    if url.startswith("http://"):
        problems.append("VITALWATCH_URL http:// ile başlıyor — secret imzası şifresiz hat üzerinden gider.")

    if problems:
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print("")

        return 1

    print("  ✓ Yapılandırma tamam.")

    init()

    if "--test" in argv:
        # Gerçek bir hata raporlanır: hem taşıma hem temizlik sınanmış olur.
        # Sonuç okunuyor, "gönderdim" varsayılmıyor — koşulsuz başarı yazan
        # bir teşhis, ağ koptuğunda da "✓" der ve kuran kişi kurulumu çalışır
        # sanır.
        result = report(RuntimeError("vitalwatch-status --test ile üretilen sınama olayı"), block=True)

        if not _report_result("Sınama olayı", result):
            return 1

    # --vitalwatch: bağlantıyı panele hata düşürmeden sınar. --test tek kurulumu
    # doğrularken doğru; onlarca kurulumu gezen bir güncelleme döngüsünde ise
    # panele onlarca sahte hata bırakır — izleme aracının kendi gürültüsünü
    # üretmesi.
    if "--vitalwatch" in argv:
        if not _report_result("Canlılık isteği", reporter().heartbeat(block=True)):
            return 1

    print("")
    print("  Verinin ulaştığı yalnızca hub panelinden doğrulanır:")
    print('  proje satırında bağlantı durumu "Bağlı" görünmelidir.')
    print("  Hub geçersiz imzaya da 204 döner; buradan anlaşılmaz.")
    print("")

    return 0


def _update_lines(values):
    """GitHub etiketlerindeki en yeni kararlı sürüm; yeniyse uyarı.

    Uyarıdır, hata değil: çıkış kodu değişmez — güncelleme döngüsündeki
    betikler eski sürümü "bozuk kurulum" saymasın. Ağ yoksa ya da hız
    sınırına takıldıysa "denetlenemedi" yazılır. ``VITALWATCH_STATUS_OFFLINE=1``
    denetimi tamamen atlar.
    """
    try:
        flag = os.environ.get("VITALWATCH_STATUS_OFFLINE") or values.get("VITALWATCH_STATUS_OFFLINE")

        if str(flag or "").strip().lower() in ("1", "true", "yes", "on", "evet"):
            return

        latest = latest_version()

        if latest is None:
            _line("Güncel sürüm", "denetlenemedi")
            return

        _line("Güncel sürüm", latest)

        installed = _stable(__version__)

        if installed is None or _stable(latest) > installed:
            print(
                f"  ! Güncelleme var: pip install -U --force-reinstall "
                f'"vitalwatch @ git+{REPO_URL}@v{latest}"'
            )
    except Exception:  # noqa: BLE001
        _line("Güncel sürüm", "denetlenemedi")


def latest_version(timeout=UPDATE_TIMEOUT):
    """En yüksek kararlı etiket (``"0.1.2"``) ya da ``None``. Hiç fırlatmaz."""
    try:
        request = urllib.request.Request(
            TAGS_URL,
            headers={
                "User-Agent": f"vitalwatch/{__version__} (vitalwatch-status)",
                "Accept": "application/vnd.github+json",
            },
        )

        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            tags = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))

        versions = [
            _stable(tag.get("name")) for tag in tags if isinstance(tag, dict)
        ]
        versions = [version for version in versions if version is not None]

        if not versions:
            return None

        return ".".join(str(part) for part in max(versions))
    except Exception:  # noqa: BLE001
        return None


def _stable(name):
    if not isinstance(name, str):
        return None

    match = _STABLE.fullmatch(name.strip())

    return tuple(int(part) for part in match.groups()) if match else None


def _report_result(label, result):
    if result and result.get("sent"):
        print(f"  ✓ {label} gönderildi (HTTP {result.get('status')}).")

        return True

    reason = (result or {}).get("error") or f"HTTP {(result or {}).get('status')}"
    print(f"  ✗ {label} GÖNDERİLEMEDİ: {reason}", file=sys.stderr)
    print("")

    return False


if __name__ == "__main__":
    sys.exit(main())
