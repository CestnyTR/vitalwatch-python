"""Log satırını hub'a gidecek olaya çevirir: maskeleme + gruplama temizliği.

Sıra önemlidir (sözleşme, 22-sunucu-ajani "Maskeleme"):

1. ``client:`` / ``referrer:`` / ``host:`` / ``server:`` / ``request:`` alanları
   ve Apache'nin ``[client …]`` / ``, referer: …`` parçaları **tamamen** silinir.
   ``host`` saldırganın yazdığı bir başlıktır; ``request`` ise ayrıca ``route``
   alanına taşınır — mesajda kalsaydı her URL ayrı hata olurdu.
2. URL ve yollardaki query string ve fragment atılır (M3).
3. IPv6 ve IPv4 adresleri (bağlantı noktasıyla birlikte) ``[ip]`` olur (M1).
4. En son paketin ``scrubber.text()`` maskesi (e-posta, kart, telefon, TC, IBAN,
   jeton) — SDK'larla aynı desenler, aynı parmak izi.

Satırın değişken önekleri (zaman damgası, PID/TID, bağlantı numarası)
ayrıştırıcıda ya da ``strip_prefix`` ile atılır; satır ortasındaki sayıları
hub'ın parmak izi zaten ``<n>``'e indiriyor.
"""

import ipaddress
import re
from urllib.parse import urlsplit

from .. import scrubber

#: Hub'ın mesaj sınırı.
MAX_MSG = 500

#: Ajanın ürettiği olay türleri (sözleşme tablosu).
AGENT_KINDS = (
    "web_server",
    "php_fpm",
    "database",
    "service_failed",
    "container_failed",
    "oom",
    "disk_full",
    "kernel",
    "log_error",
)

_QUOTED = r'"(?:[^"\\]|\\.)*"'

#: Tamamen silinen alanlar. nginx bunları mesajdan sonra ", ad: değer" olarak
#: ekler; baştaki virgül şart — "from client: …" gibi düz metin silinmesin.
#: ``client`` ve ``server`` tırnaksızdır, virgüle kadar.
_STRIP_FIELDS = [
    re.compile(r",\s*client:\s*[^,]*"),
    re.compile(r",\s*server:\s*[^,]*"),
    re.compile(r",\s*request:\s*" + _QUOTED),
    re.compile(r",\s*host:\s*" + _QUOTED),
    re.compile(r",\s*referrer:\s*" + _QUOTED),
    # Apache: ", referer: https://…" satır sonuna kadar gider.
    re.compile(r",\s*referer:\s*.*$"),
    re.compile(r"\[client [^\]]*\]\s*"),
    re.compile(r"\[remote [^\]]*\]\s*"),
]

#: URL'de query string ve fragment; düz yolda yalnızca query string atılır
#: ("/tmp/#sql_1" gibi dosya adlarında "#" fragment değildir).
_URL_QUERY = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^\s\"'?#<>]*)[?#][^\s\"'<>,)]*")
_PATH_QUERY = re.compile(r"((?<![\w/:])/[^\s\"'?#<>]*)\?[^\s\"'<>,)]*")

#: IPv6 adayı; karar ``ipaddress``'te — "10:00:00" gibi saatler IPv6 değildir.
_IPV6_BRACKETED = re.compile(r"\[([0-9A-Fa-f:.]+(?:%[\w.]+)?)\](?::\d{1,5}\b)?")
_IPV6 = re.compile(r"(?<![\w:.])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f.]*(?:%[\w.]+)?)(?![\w:])")
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]*\d)(?::\d{1,5}\b)?")

#: Satır başındaki değişken önekler. Döngüyle, değişmeyene kadar uygulanır.
_PREFIXES = [
    # [2026-10-21 10:00:00], [21-Oct-2026 10:00:00.123], [Tue Oct 21 10:00:00 2026]
    re.compile(r"^\[[^\]]*\d{1,2}:\d{2}(?::\d{2})?[^\]]*\]\s*"),
    # 2026-10-21T10:00:00.123Z / 2026-10-21 10:00:00,123 +0300 / UTC, CEST.
    # Saat dilimi kısaltması sınırlı: "ERROR" gibi seviye adları yutulmasın.
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|\s?[+-]\d{2}:?\d{2})?(?:\s+(?:UTC|GMT|[A-Z]{1,3}[SD]?T)\b)?\s*"),
    # 2026/10/21 10:00:00
    re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\s*"),
    # syslog: Oct 21 10:00:00 sunucu-adi
    re.compile(r"^[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2}(?: \S+)?\s+"),
    # nginx PID#TID: ve bağlantı numarası *5678
    re.compile(r"^\d+#\d+:\s*"),
    re.compile(r"^\*\d+\s+"),
    # [1234], [pid 1234:tid 5678]
    re.compile(r"^\[(?:pid )?\d+(?::tid \d+)?\]\s*"),
]

_REQUEST = re.compile(r"^([A-Z]{3,10})\s+(\S+)(?:\s+HTTP/[\d.]+)?$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")


def _ipv6(match):
    candidate = match.group(1)

    if candidate.count(":") < 2:
        return match.group(0)

    try:
        ipaddress.IPv6Address(candidate.split("%")[0].rstrip("."))
    except ValueError:
        return match.group(0)

    return "[ip]"


def _ipv4(match):
    try:
        ipaddress.IPv4Address(match.group(1))
    except ValueError:
        return match.group(0)

    return "[ip]"


def mask_ips(value):
    """IPv6 ve IPv4 adreslerini (varsa bağlantı noktasıyla) ``[ip]`` yapar."""
    value = _IPV6_BRACKETED.sub(_ipv6, value)
    value = _IPV6.sub(_ipv6, value)

    return _IPV4.sub(_ipv4, value)


def strip_query(value):
    """URL ve yollardaki query string ile fragment'ı atar (M3)."""
    return _PATH_QUERY.sub(r"\1", _URL_QUERY.sub(r"\1", value))


def strip_prefix(line):
    """Zaman damgası, PID ve bağlantı numarası öneklerini atar."""
    previous = None
    result = line.strip()

    while previous != result:
        previous = result

        for pattern in _PREFIXES:
            result = pattern.sub("", result, count=1)

    return result


def clean(value):
    """Maskeleme adımları 1-3; ``scrubber`` adımı ``build_event``'te."""
    result = str(value)

    for pattern in _STRIP_FIELDS:
        result = pattern.sub("", result)

    result = strip_query(result)
    result = mask_ips(result)

    return re.sub(r"\s+", " ", result).strip(" ,")


def parse_request(value):
    """``"GET /api/x?token=abc HTTP/1.1"`` → ``("GET /api/x", "/api/x")``.

    Query string ve fragment atılır; yol scrubber'dan geçer. Çözülemezse
    ``(None, None)``.
    """
    if not value:
        return None, None

    match = _REQUEST.match(value.strip())

    if not match:
        return None, None

    method, target = match.groups()

    try:
        target = urlsplit(target).path or target.split("?")[0].split("#")[0]
    except ValueError:
        target = target.split("?")[0].split("#")[0]

    if not target.startswith("/"):
        return None, None

    path = scrubber.path(mask_ips(target))

    if not path:
        return None, None

    return f"{method} {path}", path


def site_name(value):
    """Alan adı gibi görünüyorsa küçük harfli hâli; değilse ``None``.

    IP, ``_`` (nginx varsayılan sunucusu) ve ``localhost`` site sayılmaz.
    """
    if not value:
        return None

    name = str(value).strip().strip('"').lower()
    name = re.sub(r":\d+$", "", name)

    if not name or name in ("_", "localhost") or "." not in name:
        return None

    if mask_ips(name) != name or not _DOMAIN.match(name):
        return None

    return name


def build_event(kind, body, file, site=None, request=None, status=None, command=None):
    """Sözleşmedeki olay biçimi. Boş mesaj ``None`` döner.

    :param body: öneki atılmış ham mesaj
    :param request: ham istek satırı (``"GET /x?a=1 HTTP/1.1"``) — yalnızca
        yol çıkarılır, query string hiçbir alana girmez
    """
    if kind not in AGENT_KINDS:
        kind = "log_error"

    text = clean(body)

    if not text:
        return None

    site = site_name(site)

    if site:
        text = f"[{site}] {text}"

    msg = scrubber.text(text, MAX_MSG)

    if not msg or not msg.strip():
        return None

    event = {"kind": kind, "msg": msg, "file": file}
    route, path = parse_request(request)

    if route:
        event["route"] = route
        event["path"] = path

    if status:
        event["status"] = int(status)

    if command:
        event["command"] = scrubber.text(mask_ips(str(command)), 200)

    return event
