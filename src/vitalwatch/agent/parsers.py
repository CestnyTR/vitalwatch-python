"""Log biçimi başına bir ayrıştırıcı.

Her fonksiyon ``(line, file)`` alır; atlanacak satırda ``None``, raporlanacak
satırda ``normalize.build_event`` çıktısı döner. Ayrıştırıcı satırın **önekini**
(zaman damgası, PID, bağlantı numarası) ve seviyesini çözer, gürültüyü eler;
maskeleme ``build_event``'te tek yerde yapılır.

Gürültü eleme bilinçli olarak sıkı: bir web sunucusunun error log'u hata
olmayan satırlarla doludur (tarayıcıların 404'leri, TLS el sıkışma hataları,
PHP uyarıları). Bunlar gönderilseydi panel gerçek arızayı gömerdi.
"""

import json
import re

from .normalize import build_event, strip_prefix

# --------------------------------------------------------------------------- #
# Ortak
# --------------------------------------------------------------------------- #

_PLESK_SITE = re.compile(r"/var/www/vhosts/system/([^/]+)/logs/")

#: FastCGI / mod_proxy_fcgi üzerinden gelen PHP çıktısı: yalnızca ölümcül olanlar.
#: Uyarı/Notice/Deprecated uygulamanın kendi SDK'sının işi ve çok gürültülü.
_PHP_FATAL = re.compile(r"PHP (?:Fatal error|Parse error)|Uncaught |Allowed memory size .* exhausted|Maximum execution time")
_PHP_MESSAGE = re.compile(r"PHP message:|FastCGI sent in stderr|Got error 'PHP|Primary script unknown")


def plesk_site(file):
    """Plesk vhost log yolundan site adı: ``/var/www/vhosts/system/<site>/logs/…``."""
    match = _PLESK_SITE.search(file or "")

    return match.group(1) if match else None


def _quoted_field(text, name):
    match = re.search(r"\b" + name + r':\s*"((?:[^"\\]|\\.)*)"', text)

    return match.group(1) if match else None


def _bare_field(text, name):
    match = re.search(r"\b" + name + r":\s*([^,\s]+)", text)

    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# nginx (ve Plesk proxy_error_log)
# --------------------------------------------------------------------------- #

_NGINX = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[(\w+)\] \d+#\d+: (?:\*\d+ )?(.*)$")
_NGINX_LEVELS = ("error", "crit", "alert", "emerg")

#: Hata seviyesinde yazılan ama arıza olmayan satırlar.
_NGINX_NOISE = re.compile(
    # Yeni nginx sürümleri open() yerine openat() yazıyor; ikisi de 404 taraması.
    r"open(?:at)?\(\) \".*\" failed \(2: No such file or directory\)"
    r"|failed \(13: Permission denied\).*\.(?:php|env|git)"
    r"|directory index of .* is forbidden"
    r"|access forbidden by rule"
    r"|client intended to send too large body"
    r"|limiting (?:requests|connections)"
    r"|client prematurely closed"
    r"|client sent invalid"
    r"|SSL_do_handshake\(\) failed|SSL_read\(\) failed|SSL_write\(\) failed"
    r"|no \"ssl_certificate\" is defined"
    r"|is not found \(2: No such file or directory\)"
    r"|user \".*\" was not found in"
    r"|no user/password was provided"
)


def nginx_status(message):
    """Mesajdan anlaşılan HTTP durumu: zaman aşımı 504, upstream hatası 502."""
    if "upstream timed out" in message:
        return 504

    if re.search(
        r"connect\(\) (?:to .* )?failed|no live upstreams|upstream prematurely closed"
        r"|upstream sent (?:too big header|invalid)|while reading response header from upstream"
        r"|recv\(\) failed|while connecting to upstream",
        message,
    ):
        return 502

    return None


def parse_nginx(line, file, site=None):
    match = _NGINX.match(line.strip())

    if not match:
        return None

    level, message = match.groups()

    if level not in _NGINX_LEVELS or _NGINX_NOISE.search(message):
        return None

    if _PHP_MESSAGE.search(message) and not _PHP_FATAL.search(message):
        return None

    # server_name yapılandırmadan gelir ve kararlıdır; host başlığını istemci
    # yazar — tarayıcılar her rastgele host için ayrı bir "site" açardı.
    return build_event(
        "web_server",
        message,
        file,
        site=site or _bare_field(message, "server"),
        request=_quoted_field(message, "request"),
        status=nginx_status(message),
    )


def parse_plesk_proxy(line, file):
    """Plesk ``proxy_error_log``: nginx biçimi, site adı yoldan."""
    return parse_nginx(line, file, site=plesk_site(file))


# --------------------------------------------------------------------------- #
# Apache (ve Plesk error_log)
# --------------------------------------------------------------------------- #

_APACHE = re.compile(
    r"^\[[^\]]+\] \[(?:([\w-]+):)?(\w+)\] (?:\[pid [^\]]*\] )?(?:\[client [^\]]*\] |\[remote [^\]]*\] )*(.*)$"
)
_APACHE_LEVELS = ("error", "crit", "alert", "emerg")

_APACHE_NOISE = re.compile(
    r"AH01630|AH01797|AH01276|AH00128|AH01264|AH02032|AH01071: Got error 'Primary script unknown'"
    r"|client denied by server configuration|File does not exist|script not found or unable to stat"
    r"|Cannot serve directory|SSL Library Error|AH01964|AH02042|AH02261|AH01618|AH01617"
    # ModSecurity'nin engellediği istek (/.env, /.git/ taraması, XSS deseni) bir arıza
    # değil, korumanın ÇALIŞTIĞINI gösterir; Plesk sunucusunda günde yüzlerce satır.
    # 24 Ekim 2026 ilk gün ölçümü: 155 olayın ~120'si buydu.
    r"|ModSecurity: (?:Access denied|Warning)"
)


def parse_apache(line, file, site=None):
    match = _APACHE.match(line.strip())

    if not match:
        return None

    module, level, message = match.groups()

    if level not in _APACHE_LEVELS or _APACHE_NOISE.search(message):
        return None

    if _PHP_MESSAGE.search(message) and not _PHP_FATAL.search(message):
        return None

    status = None

    # Yalnızca arka uca ulaşılamadığı belli olan proxy hataları; PHP'nin kendi
    # ölümcül hatası 500'dür ve durum tahmini yapılmaz.
    if module and module.startswith("proxy"):
        if re.search(r"timeout specified has expired|timed out|AH01102.*timeout", message, re.IGNORECASE):
            status = 504
        elif re.search(
            r"AH01079|AH01102|AH00957|AH01114|AH01075|AH01097|failed to make connection|Connection refused"
            r"|error reading (?:status line|from remote server)|No such file or directory",
            message,
        ):
            status = 502

    if module:
        message = f"[{module}] {message}"

    return build_event("web_server", message, file, site=site or plesk_site(file), status=status)


def parse_plesk_error(line, file):
    """Plesk vhost ``error_log``: Apache biçimi, site adı yoldan."""
    return parse_apache(line, file, site=plesk_site(file))


# --------------------------------------------------------------------------- #
# PHP-FPM
# --------------------------------------------------------------------------- #

_FPM = re.compile(r"^\[\d{2}-\w{3}-\d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?\] (\w+): (.*)$")
_FPM_WARNING = re.compile(
    r"max_children|seems busy|exited on signal|execution timed out|request_terminate_timeout"
    r"|failed|unable to|Too many open files|out of memory",
    re.IGNORECASE,
)


def parse_php_fpm(line, file):
    match = _FPM.match(line.strip())

    if not match:
        return None

    level, message = match.groups()
    level = level.upper()

    if level in ("NOTICE", "DEBUG"):
        return None

    # "child 123 said into stderr" uygulama çıktısıdır; yalnızca ölümcül olanı.
    if "said into stderr" in message and not _PHP_FATAL.search(message):
        return None

    if level == "WARNING" and not _FPM_WARNING.search(message):
        return None

    # "child 1234 exited on signal 11" — PID gruplamayı bozmasın.
    message = re.sub(r"\bchild \d+\b", "child <n>", message)

    return build_event("php_fpm", message, file, site=plesk_site(file))


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #

_POSTGRES = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?(?: [A-Z+\-0-9]+)? \[\d+\](?:-\d+)?:? (?:\S*@\S* )?"
    r"(DEBUG\d?|LOG|INFO|NOTICE|WARNING|ERROR|FATAL|PANIC|DETAIL|HINT|STATEMENT|CONTEXT|QUERY):\s+(.*)$"
)
_POSTGRES_ERROR = re.compile(
    r"deadlock detected|out of memory|No space left|could not (?:write|extend|fsync|open file|read block)"
    r"|too many connections|corrupt|invalid page",
    re.IGNORECASE,
)
_POSTGRES_AUTH = re.compile(
    r"password authentication failed|no pg_hba\.conf entry|role \".*\" does not exist"
    r"|database \".*\" does not exist|authentication failed|SSL|unsupported frontend protocol"
    r"|invalid length of startup packet|connection to client lost|terminating connection due to administrator command"
    r"|the database system is (?:starting up|shutting down)",
    re.IGNORECASE,
)


def _sql_literals(message):
    """Tek tırnaklı değerler ``?`` olur — hata mesajı kullanıcı verisi taşıyabilir."""
    return re.sub(r"'(?:[^']|'')*'", "?", message)


def parse_postgresql(line, file):
    match = _POSTGRES.match(line.strip())

    if not match:
        return None

    level, message = match.groups()

    if level in ("FATAL", "PANIC"):
        if _POSTGRES_AUTH.search(message):
            return None
    elif level == "ERROR":
        # Uygulama düzeyi SQL hataları (sözdizimi, benzersizlik) SDK'nın işi.
        if not _POSTGRES_ERROR.search(message):
            return None
    else:
        return None

    return build_event("database", f"{level}: {_sql_literals(message)}", file)


# --------------------------------------------------------------------------- #
# MySQL / MariaDB
# --------------------------------------------------------------------------- #

_MYSQL = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?|\d{6} {1,2}\d{1,2}:\d{2}:\d{2})"
    r"\s+(?:\d+\s+)?\[(\w+)\]\s+(.*)$"
)
_MYSQL_NOISE = re.compile(r"Access denied|Aborted connection|Got an error reading communication packets|Got timeout reading")


def parse_mysql(line, file):
    match = _MYSQL.match(line.strip())

    if not match:
        return None

    level, message = match.groups()

    if level.upper() != "ERROR" or _MYSQL_NOISE.search(message):
        return None

    return build_event("database", _sql_literals(message), file)


# --------------------------------------------------------------------------- #
# MongoDB (JSON satır, 4.4+)
# --------------------------------------------------------------------------- #


def parse_mongodb(line, file):
    line = line.strip()

    if not line.startswith("{"):
        return None

    try:
        entry = json.loads(line)
    except ValueError:
        return None

    if not isinstance(entry, dict) or entry.get("s") not in ("E", "F"):
        return None

    message = str(entry.get("msg") or "").strip()

    if not message:
        return None

    component = entry.get("c")

    if component:
        message = f"{component}: {message}"

    # attr belge içeriği ve uzak adres taşıyabilir; yalnızca hata açıklaması alınır.
    attr = entry.get("attr") if isinstance(entry.get("attr"), dict) else {}
    error = attr.get("error")

    if isinstance(error, dict):
        detail = error.get("errmsg") or error.get("codeName")

        if detail:
            message = f"{message}: {detail}"
    elif isinstance(error, str):
        message = f"{message}: {error}"

    return build_event("database", _sql_literals(message), file)


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #

_REDIS = re.compile(r"^(?:\d+:[XCSM] |\[\d+\] )\d{1,2} \w{3}(?: \d{4})? \d{2}:\d{2}:\d{2}(?:\.\d+)? ([.\-*#]) (.*)$")


def parse_redis(line, file):
    match = _REDIS.match(line.strip())

    if not match:
        return None

    level, message = match.groups()

    # "#" uyarı seviyesi. "WARNING overcommit_memory…" gibi açılış tavsiyeleri
    # her yeniden başlatmada tekrarlanır ve arıza değildir.
    if level != "#" or message.startswith("WARNING"):
        return None

    return build_event("database", message, file)


# --------------------------------------------------------------------------- #
# Kullanıcı tanımlı log
# --------------------------------------------------------------------------- #


def make_custom_parser(pattern, kind="log_error"):
    """``[log:<ad>]`` bölümü: desen eşleşen satır olay olur."""

    def parse_custom(line, file):
        if not pattern.search(line):
            return None

        return build_event(kind, strip_prefix(line), file)

    return parse_custom


# --------------------------------------------------------------------------- #
# Kaynak tablosu
# --------------------------------------------------------------------------- #

#: (kaynak adı, [(glob, ayrıştırıcı), …]) — sözleşmedeki "Kaynaklar" tablosu.
FILE_SOURCES = [
    ("nginx", [("/var/log/nginx/*error*.log", parse_nginx)]),
    ("apache", [("/var/log/apache2/*error*.log", parse_apache), ("/var/log/httpd/*error*", parse_apache)]),
    (
        "plesk",
        [
            ("/var/www/vhosts/system/*/logs/error_log", parse_plesk_error),
            ("/var/www/vhosts/system/*/logs/proxy_error_log", parse_plesk_proxy),
        ],
    ),
    (
        "php_fpm",
        [
            ("/var/log/php*-fpm.log", parse_php_fpm),
            ("/var/log/php-fpm/*.log", parse_php_fpm),
            ("/opt/plesk/php/*/var/log/php-fpm.log", parse_php_fpm),
            # Plesk (Ubuntu/Debian) ana süreç günlüğünü burada tutuyor; yukarıdaki yol boş kalıyor.
            ("/var/log/plesk-php*-fpm/error.log", parse_php_fpm),
        ],
    ),
    ("postgresql", [("/var/log/postgresql/*.log", parse_postgresql)]),
    (
        "mysql",
        [
            ("/var/log/mysql/error.log", parse_mysql),
            ("/var/log/mariadb/*.log", parse_mysql),
            ("/var/log/mysqld.log", parse_mysql),
        ],
    ),
    ("mongodb", [("/var/log/mongodb/mongod.log", parse_mongodb)]),
    ("redis", [("/var/log/redis/*.log", parse_redis)]),
]
