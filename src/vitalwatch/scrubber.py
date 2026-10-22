"""Personal data scrubbing — applied BEFORE sending.

The hub repeats the same scrubbing. Both are needed: the hub defends itself,
while the package never puts personal data on the wire at all. If there is
going to be a leak, this is the first line of defence.

The patterns must behave exactly like the sibling packages
(``cestnytr/vitalwatch-laravel`` PHP, ``vitalwatch-node``): if they diverge,
the same error is masked differently in two SDKs, the fingerprint splits and a
single error shows up as two rows in the panel.

The one deliberate deviation is Unicode property escapes: Python's ``re``
module does not support ``\\p{L}``. The equivalents are built with ``\\w`` /
``[^\\W\\d_]`` — ``re`` str patterns are Unicode-aware by default, so
non-ASCII letters are covered too. The only difference is the underscore in
an email domain (this package masks that as well); on realistic input the
behaviour is the same.
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
    """Mask and truncate. An empty value returns ``None`` — no empty fields are sent."""
    if value is None or value == "":
        return None

    result = str(value)

    for pattern, replacement in PATTERNS:
        result = pattern.sub(replacement, result)

    return result[:limit]


def path(value):
    """Drop the query string and fragment; only the path is kept (M3)."""
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
    """Normalize SQL: literal values are replaced with ``?``.

    ``where email = 'jane@example.com'`` → ``where email = ?``

    Right for both privacy law (KVKK/GDPR) and grouping: the same query run
    with different parameters collapses into one fingerprint.
    """
    if not value:
        return None

    result = str(value)

    for pattern, replacement in _SQL_LITERALS:
        result = pattern.sub(replacement, result)

    return text(re.sub(r"\s+", " ", result).strip(), 500)


def message(value, contains_sql=False):
    """Scrub an error message.

    Database driver errors carry the SQL with bound values; the SQL is
    normalized first, then general masking is applied. Cleaning only the query
    field was not enough — the real leak came from the message itself.
    """
    if not value:
        return None

    result = str(value)

    if contains_sql:
        result = re.sub(r"'(?:[^']|'')*'", "?", result)
        result = re.sub(r'"(?:[^"]|"")*"(\s*=\s*)\S+', r'"?"\1?', result)

    return text(result, 500)


def stack(value):
    """Truncate and mask a traceback.

    site-packages frames are kept — the whole chain is needed to find where
    the error happened — but the length is capped.
    """
    return text(value, 2000)
