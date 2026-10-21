"""Yapılandırmayı süreç ortamından ve ``.env`` dosyalarından okur.

``.env`` okuması gereksiz görünebilir — çoğu uygulama zaten okuyor. Ama iki
durumda okumuyor ve ikisi de sessiz arıza üretir:

1. **Erken kurulum.** ``vitalwatch`` uygulama ``load_dotenv()`` çağırmadan önce
   import edilirse ``os.environ`` boştur; paket "yapılandırma eksik" deyip
   hiçbir kanca kurmaz ve hiçbir şey söylemez.
2. **``vitalwatch-status``.** Ayrı bir süreç; uygulamanın ``.env``'i hiç yüklenmez.
   Gerçek bir kurulumda ``.env`` doğru doldurulmuşken komutun "TANIMSIZ"
   demesi, teşhis aracının yanlış teşhis koymasıdır — hiç teşhis koymamaktan
   kötüdür.

Öncelik: ``os.environ`` > ``.env.<ortam>`` > ``.env.local`` > ``.env``.
Süreç ortamı **her zaman kazanır**; dosya yalnızca boşluğu doldurur.

Yalnızca ``VITALWATCH_`` ile başlayan anahtarlar okunur — paketin başka hiçbir
değişkeni görmesine gerek yok. Bu aynı zamanda bir güvenlik sınırı: paket
uygulamanın veritabanı şifresini hiç görmez.
"""

import os

PREFIX = "VITALWATCH_"

_cache = None


def env():
    """Birleşik yapılandırma sözlüğü. Sonuç önbelleklenir.

    :rtype: dict[str, str]
    """
    global _cache

    if _cache is not None:
        return _cache

    values = {}

    # Sondan başa: önce en zayıf kaynak yazılır, güçlü olan üzerine yazar.
    for name in reversed(_files()):
        values.update(_read(name))

    for key, value in os.environ.items():
        if key.startswith(PREFIX) and value != "":
            values[key] = value

    _cache = values

    return values


def _files():
    """Güçlüden zayıfa ``.env`` dosya adları."""
    # FLASK_ENV, Flask 2.3'te kaldırıldı ama .env dosyalarında hâlâ yaygın;
    # APP_ENV kardeş Laravel paketiyle aynı isim.
    name = os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV")

    return [f".env.{name}" if name else None, ".env.local", ".env"]


def _read(name):
    """Küçük bir ``.env`` ayrıştırıcı.

    Bağımlılık politikası gereği ``python-dotenv`` eklenmiyor. Desteklenenler:
    ``ANAHTAR=deger``, tırnaklı değer, ``#`` yorum, ``export `` öneki. ``.env``
    sözdiziminin tamamı değil ama bu paketin okuduğu yedi değişken için
    fazlasıyla yeterli.
    """
    values = {}

    if not name:
        return values

    try:
        with open(os.path.join(os.getcwd(), name), "r", encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, ValueError):
        # Dosya yoksa, okunamıyorsa ya da UTF-8 değilse (UnicodeDecodeError bir
        # ValueError) sessizce geç: Windows'ta cp1254 kaydedilmiş bir .env
        # uygulamanın açılışını düşürüyordu.
        return values

    for raw in content.splitlines():
        line = raw.strip()

        if line.startswith("export "):
            line = line[len("export ") :].strip()

        if not line or line.startswith("#"):
            continue

        separator = line.find("=")

        if separator < 1:
            continue

        key = line[:separator].strip()

        if not key.startswith(PREFIX):
            continue

        value = line[separator + 1 :].strip()

        # Tırnak içindeyse tırnaklar atılır; değilse satır sonu yorumu kesilir.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split(" #")[0].strip()

        if value != "":
            values[key] = value

    return values


def forget():
    """Test için: önbelleği düşürür."""
    global _cache

    _cache = None
