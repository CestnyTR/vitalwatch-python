"""Ajan testleri için gerçek biçimli örnek satırlar.

IP'ler belgelendirme blokları (RFC 5737 / RFC 3849), alan adları
``example.com``. Sırlar ve jetonlar sahte; sağlayıcı öneki taşımaz.
"""

NGINX_TIMEOUT = (
    "2026/10/21 10:00:00 [error] 1234#1234: *5678 upstream timed out (110: Connection timed out) while reading "
    'response header from upstream, client: 203.0.113.7, server: example.com, request: "GET /api/x?token=abc HTTP/1.1", '
    'upstream: "fastcgi://unix:/run/php/php8.4-fpm.sock:", host: "example.com", referrer: "https://example.com/?q=1"'
)
NGINX_CONNECT_REFUSED = (
    "2026/10/21 10:00:01 [error] 1234#1234: *5679 connect() failed (111: Connection refused) while connecting to "
    'upstream, client: 2001:db8::7, server: shop.example.com, request: "POST /checkout?step=2#pay HTTP/2.0", '
    'upstream: "http://127.0.0.1:8000/checkout?step=2", host: "shop.example.com:443", referrer: "https://shop.example.com/cart?id=9"'
)
NGINX_NO_SOCKET = (
    "2026/10/21 10:00:02 [crit] 1234#1234: *5680 connect() to unix:/run/php/php8.4-fpm.sock failed "
    "(2: No such file or directory) while connecting to upstream, client: 198.51.100.4, server: _, "
    'request: "GET / HTTP/1.1", upstream: "fastcgi://unix:/run/php/php8.4-fpm.sock:", host: "198.51.100.1"'
)
NGINX_404_NOISE = (
    '2026/10/21 10:00:03 [error] 1234#1234: *5681 open() "/var/www/html/wp-login.php" failed '
    '(2: No such file or directory), client: 203.0.113.9, server: example.com, request: "GET /wp-login.php HTTP/1.1", host: "example.com"'
)
NGINX_SSL_NOISE = (
    "2026/10/21 10:00:04 [crit] 1234#1234: *5682 SSL_do_handshake() failed (SSL: error:0A00006C:SSL routines::bad key share) "
    "while SSL handshaking, client: 203.0.113.10, server: 0.0.0.0:443"
)
NGINX_WARN = (
    "2026/10/21 10:00:05 [warn] 1234#1234: *5683 an upstream response is buffered to a temporary file "
    '/var/cache/nginx/fastcgi_temp/1/00/0000000001 while reading upstream, client: 203.0.113.7, server: example.com, request: "GET /x HTTP/1.1"'
)
NGINX_PHP_WARNING = (
    '2026/10/21 10:00:06 [error] 1234#1234: *5684 FastCGI sent in stderr: "PHP message: PHP Warning:  Undefined variable $x in '
    '/var/www/app/index.php on line 3" while reading response header from upstream, client: 203.0.113.7, server: example.com, '
    'request: "GET /?a=1 HTTP/1.1", upstream: "fastcgi://unix:/run/php/php8.4-fpm.sock:", host: "example.com"'
)
NGINX_PHP_FATAL = (
    '2026/10/21 10:00:07 [error] 1234#1234: *5685 FastCGI sent in stderr: "PHP message: PHP Fatal error:  Allowed memory size '
    "of 134217728 bytes exhausted (tried to allocate 20480 bytes) in /var/www/app/vendor/x.php on line 10\" while reading "
    'response header from upstream, client: 203.0.113.7, server: example.com, request: "GET /report?user=ali@example.com HTTP/1.1", '
    'upstream: "fastcgi://unix:/run/php/php8.4-fpm.sock:", host: "example.com", referrer: "https://example.com/?ref=mail"'
)

APACHE_PROXY = (
    "[Tue Oct 21 10:00:00.123456 2026] [proxy_fcgi:error] [pid 1234:tid 140000000000000] (70007)The timeout specified has "
    "expired: [client 203.0.113.7:51234] AH01075: Error dispatching request to : (polling), referer: https://example.com/?q=1"
)
APACHE_MAXWORKERS = (
    "[Tue Oct 21 10:00:01.000001 2026] [mpm_prefork:error] [pid 999] AH00161: server reached MaxRequestWorkers setting, "
    "consider raising the MaxRequestWorkers setting"
)
APACHE_DENIED_NOISE = (
    "[Tue Oct 21 10:00:02.000002 2026] [authz_core:error] [pid 1234:tid 1400] [client 203.0.113.7:51235] "
    "AH01630: client denied by server configuration: /var/www/html/.env"
)
APACHE_NOTICE = "[Tue Oct 21 10:00:03.000003 2026] [mpm_event:notice] [pid 1:tid 1] AH00489: Apache/2.4.62 configured -- resuming normal operations"
APACHE_PHP_FATAL = (
    "[Tue Oct 21 10:00:04.000004 2026] [proxy_fcgi:error] [pid 1234:tid 1400] [client 2001:db8::9:51236] AH01071: Got error "
    "'PHP message: PHP Fatal error:  Uncaught Error: Call to undefined function foo() in /var/www/vhosts/example.com/httpdocs/index.php:3', "
    "referer: https://example.com/page?token=abcdef"
)

PLESK_ERROR_LOG = "/var/www/vhosts/system/example.com/logs/error_log"
PLESK_PROXY_LOG = "/var/www/vhosts/system/example.com/logs/proxy_error_log"

FPM_MAX_CHILDREN = "[21-Oct-2026 10:00:00] WARNING: [pool www] server reached pm.max_children setting (5), consider raising it"
FPM_SIGSEGV = "[21-Oct-2026 10:00:01] WARNING: [pool www] child 12345 exited on signal 11 (SIGSEGV - core dumped) after 3.5 seconds from start"
FPM_NOTICE = "[21-Oct-2026 10:00:02] NOTICE: [pool www] child 12346 started"
FPM_EXITED_OK = "[21-Oct-2026 10:00:03.123456] NOTICE: [pool www] child 12345 exited with code 0 after 100.0 seconds from start"
FPM_STDERR = '[21-Oct-2026 10:00:04] WARNING: [pool www] child 12347 said into stderr: "NOTICE: PHP message: PHP Deprecated:  x"'
FPM_ERROR = "[21-Oct-2026 10:00:05] ERROR: unable to bind listening socket for address '/run/php/php8.4-fpm.sock': Address already in use (98)"

PG_SLOTS = "2026-10-21 10:00:00.123 UTC [1234] FATAL:  remaining connection slots are reserved for non-replication superuser connections"
PG_USER_PREFIX = "2026-10-21 10:00:01.456 +03 [2345] app@shop FATAL:  sorry, too many clients already"
PG_DEADLOCK = "2026-10-21 10:00:02.789 UTC [3456] app@shop ERROR:  deadlock detected"
PG_DETAIL = "2026-10-21 10:00:02.789 UTC [3456] app@shop DETAIL:  Process 3456 waits for ShareLock on transaction 99; blocked by process 4567."
PG_APP_ERROR = "2026-10-21 10:00:03.000 UTC [3457] app@shop ERROR:  duplicate key value violates unique constraint \"users_email_key\""
PG_AUTH = '2026-10-21 10:00:04.000 UTC [3458] FATAL:  no pg_hba.conf entry for host "203.0.113.7", user "postgres", database "postgres", SSL off'
PG_LOG = "2026-10-21 10:00:05.000 UTC [1] LOG:  database system is ready to accept connections"
PG_PANIC = "2026-10-21 10:00:06.000 UTC [1] PANIC:  could not write to file \"pg_wal/xlogtemp.1\": No space left on device"

MYSQL8_ERROR = (
    "2026-10-21T10:00:00.123456Z 12 [ERROR] [MY-012592] [InnoDB] Operating system error number 28 in a file operation."
)
MYSQL8_WARNING = "2026-10-21T10:00:01.123456Z 13 [Warning] [MY-010055] [Server] IP address '203.0.113.7' could not be resolved"
MARIADB_ERROR = "2026-10-21 10:00:02 0 [ERROR] mysqld: Table './shop/orders' is marked as crashed and should be repaired"
MARIADB_ACCESS = "2026-10-21 10:00:03 15 [ERROR] Access denied for user 'root'@'203.0.113.7' (using password: YES)"
MYSQL_LEGACY = "161021 10:00:04 [ERROR] /usr/sbin/mysqld: Disk full (/tmp/#sql_1); waiting for someone to free some space..."

MONGO_ERROR = (
    '{"t":{"$date":"2026-10-21T10:00:00.000+00:00"},"s":"E","c":"NETWORK","id":23077,"ctx":"conn1",'
    '"msg":"Error receiving request from client","attr":{"error":{"code":141,"codeName":"SSLHandshakeFailed",'
    '"errmsg":"SSL handshake received but server is started without SSL support"},"remote":"203.0.113.7:51234","connectionId":1}}'
)
MONGO_FATAL = (
    '{"t":{"$date":"2026-10-21T10:00:01.000+00:00"},"s":"F","c":"STORAGE","id":28595,"ctx":"initandlisten",'
    '"msg":"Terminating.","attr":{"reason":"28: No space left on device"}}'
)
MONGO_INFO = (
    '{"t":{"$date":"2026-10-21T10:00:02.000+00:00"},"s":"I","c":"NETWORK","id":22943,"ctx":"listener",'
    '"msg":"Connection accepted","attr":{"remote":"203.0.113.7:51235","connectionId":2}}'
)

REDIS_ERROR = "1234:M 21 Oct 2026 10:00:00.000 # Error moving temp DB file temp-1234.rdb on the final destination dump.rdb (in server root dir /var/lib/redis): Read-only file system"
REDIS_BGSAVE = "1234:M 21 Oct 2026 10:00:01.123 # Background saving error"
REDIS_STARTUP_WARNING = "1234:M 21 Oct 2026 10:00:02.000 # WARNING Memory overcommit must be enabled! Without it, a background save may fail."
REDIS_NOTICE = "1234:M 21 Oct 2026 10:00:03.000 * Ready to accept connections tcp"
REDIS_LEGACY = "[1234] 21 Oct 10:00:04.000 # Can't save in background: fork: Cannot allocate memory"

# journald: journalctl -o json çıktısı (bir satır = bir kayıt)
JOURNAL_UNIT_FAILED = {
    "__CURSOR": "s=abc;i=1",
    "PRIORITY": "4",
    "SYSLOG_IDENTIFIER": "systemd",
    "_PID": "1",
    "_SYSTEMD_UNIT": "init.scope",
    "UNIT": "php8.4-fpm.service",
    "MESSAGE": "php8.4-fpm.service: Failed with result 'exit-code'.",
    "_TRANSPORT": "journal",
}
JOURNAL_MAIN_EXITED = dict(JOURNAL_UNIT_FAILED, __CURSOR="s=abc;i=2", MESSAGE="php8.4-fpm.service: Main process exited, code=exited, status=78/CONFIG")
JOURNAL_USER_UNIT_FAILED = {
    "__CURSOR": "s=abc;i=3",
    "PRIORITY": "4",
    "SYSLOG_IDENTIFIER": "systemd",
    "_PID": "2001",
    "USER_UNIT": "tracker.service",
    "MESSAGE": "tracker.service: Failed with result 'exit-code'.",
}
JOURNAL_OOM = {
    "__CURSOR": "s=abc;i=4",
    "PRIORITY": "3",
    "SYSLOG_IDENTIFIER": "kernel",
    "_TRANSPORT": "kernel",
    "MESSAGE": "Out of memory: Killed process 4321 (php-fpm8.4) total-vm:812345kB, anon-rss:654321kB, file-rss:0kB, shmem-rss:0kB, UID:33 pgtables:1234kB oom_score_adj:0",
}
JOURNAL_CGROUP_OOM = dict(JOURNAL_OOM, __CURSOR="s=abc;i=5", MESSAGE="Memory cgroup out of memory: Killed process 999 (node) total-vm:1kB")
JOURNAL_SEGFAULT = {
    "__CURSOR": "s=abc;i=6",
    "PRIORITY": "6",
    "SYSLOG_IDENTIFIER": "kernel",
    "_TRANSPORT": "kernel",
    "MESSAGE": "php-fpm8.4[12345]: segfault at 7f3a2b1c0000 ip 00007f3a2b1c1234 sp 00007ffd12345678 error 4 in libc.so.6[7f3a2b000000+195000] likely on CPU 1 (core 1, socket 0)",
}
JOURNAL_IO_ERROR = {
    "__CURSOR": "s=abc;i=7",
    "PRIORITY": "3",
    "SYSLOG_IDENTIFIER": "kernel",
    "_TRANSPORT": "kernel",
    "MESSAGE": "blk_update_request: I/O error, dev sda, sector 123456 op 0x1:(WRITE) flags 0x800 phys_seg 1 prio class 0",
}
JOURNAL_APP_ERR = {
    "__CURSOR": "s=abc;i=8",
    "PRIORITY": "3",
    "SYSLOG_IDENTIFIER": "myapp",
    "_SYSTEMD_UNIT": "myapp.service",
    "MESSAGE": "database connection lost to 10.0.0.5:5432, retrying",
}
JOURNAL_SSHD = {
    "__CURSOR": "s=abc;i=9",
    "PRIORITY": "3",
    "SYSLOG_IDENTIFIER": "sshd",
    "_SYSTEMD_UNIT": "ssh.service",
    "MESSAGE": "error: kex_exchange_identification: Connection closed by remote host 203.0.113.7 port 51234",
}
JOURNAL_BYTES_MESSAGE = {
    "__CURSOR": "s=abc;i=10",
    "PRIORITY": "3",
    "SYSLOG_IDENTIFIER": "kernel",
    "_TRANSPORT": "kernel",
    "MESSAGE": list(b"EXT4-fs error (device sda1): ext4_find_entry:1455: inode #2: comm ls: reading directory lblock 0 \xff"),
}
JOURNAL_OOMD = {
    "__CURSOR": "s=abc;i=11",
    "PRIORITY": "6",
    "SYSLOG_IDENTIFIER": "systemd-oomd",
    "MESSAGE": "Killed /system.slice/api.service due to memory pressure for /system.slice being 82.50% > 80.00% for > 20s with reclaim activity",
}


def docker_event(action, name="api", exit_code=None, at=1_761_040_000, legacy=True):
    """``docker events --format '{{json .}}'`` satırının sözlüğü."""
    attributes = {"image": "registry.example.com/api:1", "name": name}

    if exit_code is not None:
        attributes["exitCode"] = str(exit_code)

    event = {
        "Type": "container",
        "Action": action,
        "Actor": {"ID": "f00dfeed" * 8, "Attributes": attributes},
        "scope": "local",
        "time": at,
        "timeNano": at * 1_000_000_000,
    }

    if legacy:
        event.update({"status": action, "id": "f00dfeed" * 8, "from": "registry.example.com/api:1"})

    return event

# 24 Ekim 2026, canlı sunucudan (adresler örnek değerlerle değiştirildi)
NGINX_404_OPENAT_NOISE = (
    '2026/10/23 20:59:01 [error] 1234#1234: *9001 openat() "/var/www/vhosts/default/htdocs/.well-known/acme-challenge/222.php" '
    'failed (2: No such file or directory), client: 203.0.113.11, server: , request: "GET /.well-known/acme-challenge/222.php HTTP/1.1", host: "example.com"'
)
APACHE_MODSECURITY_BLOCK = (
    "[Thu Oct 23 18:42:00.000000 2026] [security2:error] [pid 2222:tid 2223] [client 203.0.113.12:40000] "
    '[client 203.0.113.12] ModSecurity: Access denied with code 403 (phase 1). Matched phrase "/.env" at REQUEST_URI. '
    '[file "/etc/apache2/modsecurity.d/rules/comodo_free/02_Global_Generic.conf"] [line "120"] [id "210492"]'
)
APACHE_MODSECURITY_WARNING = (
    "[Thu Oct 23 18:43:00.000000 2026] [security2:error] [pid 2222:tid 2224] [client 203.0.113.13:40001] "
    '[client 203.0.113.13] ModSecurity: Warning. Matched phrase "/.git/" at REQUEST_URI. [id "210492"]'
)
