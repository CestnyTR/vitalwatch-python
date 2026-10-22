"""Kişisel veri temizliği — gönderimden ÖNCE uygulanır.

Hub tarafında aynı temizlik tekrar yapılır. İkisi de gereklidir: hub kendini
savunur, paket ise kişisel veriyi ağa hiç çıkarmaz. Bir sızıntı olacaksa ilk
savunma hattı burasıdır.

Desenler kardeş paketlerle (``cestnytr/vitalwatch-laravel`` PHP, ``vitalwatch-node``)
aynı davranmak zorundadır: ayrışırlarsa aynı hata iki SDK'da farklı maskelenir,
parmak izi bölünür ve panelde tek hata iki satır olarak görünür.

Tek bilinçli sapma Unicode özellik kaçışlarındadır: Python'un ``re`` modülü
``\\p{L}`` desteklemez. Karşılıkları ``\\w`` / ``[^\\W\\d_]`` ile kuruldu —
``re`` str desenlerinde varsayılan olarak Unicode farkındadır, yani "ş", "ü"
gibi harfler de kapsanır. Fark yalnızca e-posta alan adındaki alt çizgide
kalır (bu paket onu da maskeler); gerçekçi girdide davranış aynıdır.
"""

import re
from urllib.parse import urlsplit

PATTERNS = [
    # E-posta. Gevşek bir sürüm ([^\s@]+) eğik çizgiyi de yutar ve
    # "/kullanici/a@b.com/profil" yolunun tamamını maskelerdi.
    (re.compile(r"[\w.%+-]+@[\w.-]+\.[^\W\d_]{2,}"), "[email]"),
    (re.compile(r"\bTR(?:[\s-]?\d){24}\b", re.IGNORECASE), "[iban]"),
    # Aday; karar _is_card'da — dosya adındaki zaman damgası kart sanılmasın.
    (re.compile(r"\b\d(?:[\s-]?\d){12,18}\b"), lambda m: "[card]" if _is_card(m.group(0)) else m.group(0)),
    (re.compile(r"\b[1-9]\d{10}\b"), "[national_id]"),
    # Önünde rakam olamaz: "1795123456789" damgası "179[phone]" oluyordu.
    (re.compile(r"(?<!\d)(?:\+?90|0)?[\s-]?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b"), "[phone]"),
    # Uzun rastgele diziler: oturum kimliği, API anahtarı, jeton, hash.
    # 24 hane eşiği bilinçli: oturum kimlikleri 40, API anahtarları 32+
    # karakterdir; normal kelimeler ve sınıf adları bu uzunluğa ulaşmaz.
    # Aday; karar _is_token'da — okunur dosya adları maskelenmesin.
    (re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{23,}"), lambda m: "[token]" if _is_token(m.group(0)) else m.group(0)),
]


def _is_card(candidate):
    """Gerçek kart: 2-9 ile başlar ve Luhn'dan geçer. Zaman damgası 1 ile başlar."""
    digits = re.sub(r"\D", "", candidate)
    if not digits or digits[0] < "2":
        return False

    total = 0
    double = False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double

    return total % 10 == 0


def _is_token(candidate):
    """Rastgele dizi: 16+ karakterlik bölünmemiş parça ya da harf içeren hex.

    Tireyle birleşmiş kısa parçalar okunur addır: ``kampanya-gorseli-1726571234567``.
    """
    # Düz CamelCase/camelCase ad jeton değildir: gerçek jeton ve oturum
    # kimlikleri neredeyse hep rakam taşır; rakamsız, yan yana iki büyük
    # harfi olmayan ve küçük harfle biten 24+ karakterlik rastgele dizinin
    # olasılığı 1/10.000'in çok altında. .NET'in `InvalidOperationException`
    # gibi adları maskeleniyor, yığın izleri okunmaz oluyordu.
    if re.fullmatch(r"[A-Za-z][a-z]+(?:[A-Z][a-z]+)+", candidate):
        return False

    if any(len(part) >= 16 for part in re.split(r"[-_]", candidate)):
        return True

    compact = re.sub(r"[-_]", "", candidate)

    return bool(re.fullmatch(r"[0-9a-fA-F]+", compact)) and bool(re.search(r"[a-fA-F]", compact))

_SQL_LITERALS = [
    (re.compile(r"'(?:[^']|'')*'"), "?"),
    (re.compile(r'"(?:[^"]|"")*"'), "?"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "?"),
    (re.compile(r"\b(IN)\s*\(\s*\?(?:\s*,\s*\?)+\s*\)", re.IGNORECASE), r"\1 (?)"),
]


def text(value, limit):
    """Maskele ve kırp. Boş değer ``None`` döner — hub'a boş alan gitmez."""
    if value is None or value == "":
        return None

    result = str(value)

    for pattern, replacement in PATTERNS:
        result = pattern.sub(replacement, result)

    return result[:limit]


def path(value):
    """Query string ve fragment atılır; yalnızca yol kalır (M3)."""
    if not value:
        return None

    try:
        # Taban adres yalnızca göreli yolları çözmek için; dışarı çıkmıyor.
        parsed = urlsplit(str(value))
        result = parsed.path if parsed.path else str(value)
    except ValueError:
        result = str(value).split("?")[0].split("#")[0]

    return text(result, 300)


def sql(value):
    """SQL normalize: literal değerler ``?`` ile değiştirilir.

    ``where email = 'ahmet@ornek.com'`` → ``where email = ?``

    Hem KVKK gereği hem gruplama açısından doğru: aynı sorgu farklı
    parametrelerle çalıştığında tek parmak izinde toplanır.
    """
    if not value:
        return None

    result = str(value)

    for pattern, replacement in _SQL_LITERALS:
        result = pattern.sub(replacement, result)

    return text(re.sub(r"\s+", " ", result).strip(), 500)


def message(value, contains_sql=False):
    """Hata mesajı.

    Veritabanı sürücülerinin hataları SQL'i bağlanmış değerlerle taşır; önce
    SQL normalize edilir, sonra genel maskeleme uygulanır. Yalnızca sorgu
    alanını temizlemek yetmiyordu — asıl sızıntı mesajın kendisinden oluyordu.
    """
    if not value:
        return None

    result = str(value)

    if contains_sql:
        result = re.sub(r"'(?:[^']|'')*'", "?", result)
        result = re.sub(r'"(?:[^"]|"")*"(\s*=\s*)\S+', r'"?"\1?', result)

    return text(result, 500)


def stack(value):
    """Traceback kısaltılır ve maskelenir.

    site-packages satırları atılmaz — hatanın nerede olduğunu bulmak için
    zincirin tamamı gerekir — ama uzunluk sınırlanır.
    """
    return text(value, 2000)
