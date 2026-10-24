"""Ajan: log biçimi ayrıştırıcıları ve maskeleme (gerçek biçimli örnek satırlar)."""

import fnmatch
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_fixtures as F  # noqa: E402

from vitalwatch.agent import normalize, parsers  # noqa: E402

NGINX_LOG = "/var/log/nginx/error.log"

#: Her ayrıştırıcı için (satır, parser, dosya) — sızıntı taraması bu listede döner.
ALL_LINES = [
    (value, parser, file)
    for name, value in vars(F).items()
    if isinstance(value, str)
    for prefix, parser, file in (
        ("NGINX_", parsers.parse_nginx, NGINX_LOG),
        ("APACHE_", parsers.parse_apache, "/var/log/apache2/error.log"),
        ("FPM_", parsers.parse_php_fpm, "/var/log/php8.4-fpm.log"),
        ("PG_", parsers.parse_postgresql, "/var/log/postgresql/postgresql-16-main.log"),
        ("MYSQL", parsers.parse_mysql, "/var/log/mysql/error.log"),
        ("MARIADB", parsers.parse_mysql, "/var/log/mariadb/mariadb.log"),
        ("MONGO_", parsers.parse_mongodb, "/var/log/mongodb/mongod.log"),
        ("REDIS_", parsers.parse_redis, "/var/log/redis/redis-server.log"),
    )
    if name.startswith(prefix)
]

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def assert_clean(test, event):
    """Hiçbir alanda IP, query string, referrer, client ya da host kalmamalı."""
    for key in ("msg", "route", "path", "command"):
        value = event.get(key)

        if not value:
            continue

        test.assertIsNone(_IPV4.search(value), f"{key}: IPv4 sızdı: {value}")
        test.assertNotIn("2001:db8", value, f"{key}: IPv6 sızdı")
        test.assertNotIn("?", value.replace("? ", "").replace(" ?", "").rstrip("?"), f"{key}: query string: {value}")
        test.assertNotIn("referrer", value)
        test.assertNotIn("referer", value)
        test.assertNotRegex(value, r",\s*client:")
        test.assertNotRegex(value, r",\s*host:")
        test.assertNotIn("token=", value)
        test.assertNotIn("ali@example.com", value)

    test.assertLessEqual(len(event["msg"]), 500)


class NginxTest(unittest.TestCase):
    def test_upstream_timeout(self):
        event = parsers.parse_nginx(F.NGINX_TIMEOUT, NGINX_LOG)

        self.assertEqual("web_server", event["kind"])
        self.assertEqual(
            "[example.com] upstream timed out (110: Connection timed out) while reading response header from upstream, "
            'upstream: "fastcgi://unix:/run/php/php8.4-fpm.sock:"',
            event["msg"],
        )
        self.assertEqual(NGINX_LOG, event["file"])
        self.assertEqual("GET /api/x", event["route"])
        self.assertEqual("/api/x", event["path"])
        self.assertEqual(504, event["status"])
        assert_clean(self, event)

    def test_connect_refused_ipv6_client_and_upstream_query(self):
        event = parsers.parse_nginx(F.NGINX_CONNECT_REFUSED, NGINX_LOG)

        self.assertEqual(502, event["status"])
        self.assertEqual("POST /checkout", event["route"])
        self.assertTrue(event["msg"].startswith("[shop.example.com] connect() failed"))
        self.assertIn('upstream: "http://[ip]/checkout"', event["msg"])
        assert_clean(self, event)

    def test_default_server_has_no_site_and_host_header_ignored(self):
        event = parsers.parse_nginx(F.NGINX_NO_SOCKET, NGINX_LOG)

        self.assertFalse(event["msg"].startswith("["), event["msg"])
        self.assertEqual(502, event["status"])
        self.assertEqual("GET /", event["route"])
        assert_clean(self, event)

    def test_noise_is_skipped(self):
        for line in (F.NGINX_404_NOISE, F.NGINX_404_OPENAT_NOISE, F.NGINX_SSL_NOISE, F.NGINX_WARN, F.NGINX_PHP_WARNING):
            self.assertIsNone(parsers.parse_nginx(line, NGINX_LOG), line)

    def test_php_fatal_through_fastcgi(self):
        event = parsers.parse_nginx(F.NGINX_PHP_FATAL, NGINX_LOG)

        self.assertIn("PHP Fatal error", event["msg"])
        self.assertEqual("GET /report", event["route"])
        assert_clean(self, event)

    def test_non_nginx_line(self):
        self.assertIsNone(parsers.parse_nginx("random text", NGINX_LOG))
        self.assertIsNone(parsers.parse_nginx("", NGINX_LOG))


class ApacheAndPleskTest(unittest.TestCase):
    def test_proxy_timeout_strips_client_and_referer(self):
        event = parsers.parse_apache(F.APACHE_PROXY, "/var/log/apache2/error.log")

        self.assertEqual(504, event["status"])
        self.assertTrue(event["msg"].startswith("[proxy_fcgi] "))
        self.assertIn("AH01075", event["msg"])
        assert_clean(self, event)

    def test_max_request_workers(self):
        event = parsers.parse_apache(F.APACHE_MAXWORKERS, "/var/log/httpd/error_log")

        self.assertIn("MaxRequestWorkers", event["msg"])
        self.assertNotIn("status", event)

    def test_noise_and_notice_skipped(self):
        self.assertIsNone(parsers.parse_apache(F.APACHE_DENIED_NOISE, "/var/log/apache2/error.log"))
        self.assertIsNone(parsers.parse_apache(F.APACHE_MODSECURITY_BLOCK, "/var/log/apache2/error.log"))
        self.assertIsNone(parsers.parse_apache(F.APACHE_MODSECURITY_WARNING, "/var/log/apache2/error.log"))
        self.assertIsNone(parsers.parse_plesk_error(F.APACHE_MODSECURITY_BLOCK, F.PLESK_ERROR_LOG))
        self.assertIsNone(parsers.parse_apache(F.APACHE_NOTICE, "/var/log/apache2/error.log"))

    def test_plesk_error_log_site_from_path(self):
        event = parsers.parse_plesk_error(F.APACHE_PHP_FATAL, F.PLESK_ERROR_LOG)

        self.assertTrue(event["msg"].startswith("[example.com] [proxy_fcgi] AH01071"), event["msg"])
        self.assertEqual(F.PLESK_ERROR_LOG, event["file"])
        self.assertNotIn("status", event, "PHP'nin kendi hatası 502/504 değil")
        assert_clean(self, event)

    def test_plesk_proxy_error_log_site_from_path(self):
        event = parsers.parse_plesk_proxy(F.NGINX_NO_SOCKET, F.PLESK_PROXY_LOG)

        self.assertTrue(event["msg"].startswith("[example.com] connect() to unix:"), event["msg"])
        self.assertEqual(F.PLESK_PROXY_LOG, event["file"])

    def test_plesk_site_helper(self):
        self.assertEqual("example.com", parsers.plesk_site(F.PLESK_ERROR_LOG))
        self.assertIsNone(parsers.plesk_site("/var/log/nginx/error.log"))


class PhpFpmTest(unittest.TestCase):
    FILE = "/var/log/php8.4-fpm.log"

    def test_max_children(self):
        event = parsers.parse_php_fpm(F.FPM_MAX_CHILDREN, self.FILE)

        self.assertEqual("php_fpm", event["kind"])
        self.assertEqual("[pool www] server reached pm.max_children setting (5), consider raising it", event["msg"])

    def test_signal_pid_normalized(self):
        event = parsers.parse_php_fpm(F.FPM_SIGSEGV, self.FILE)

        self.assertIn("child <n> exited on signal 11", event["msg"])

    def test_error_level(self):
        self.assertIn("unable to bind", parsers.parse_php_fpm(F.FPM_ERROR, self.FILE)["msg"])

    def test_noise_skipped(self):
        for line in (F.FPM_NOTICE, F.FPM_EXITED_OK, F.FPM_STDERR):
            self.assertIsNone(parsers.parse_php_fpm(line, self.FILE), line)

    def test_plesk_log_path_is_watched(self):
        # Plesk'in PHP-FPM günlüğü /var/log/plesk-phpXY-fpm/error.log; döndürülmüş kopyalar değil.
        globs = [g for name, pairs in parsers.FILE_SOURCES if name == "php_fpm" for g, _ in pairs]

        self.assertTrue(any(fnmatch.fnmatch("/var/log/plesk-php84-fpm/error.log", g) for g in globs))
        self.assertFalse(any(fnmatch.fnmatch("/var/log/plesk-php84-fpm/error.log.1", g) for g in globs))


class DatabaseTest(unittest.TestCase):
    PG = "/var/log/postgresql/postgresql-16-main.log"

    def test_postgres_fatal(self):
        event = parsers.parse_postgresql(F.PG_SLOTS, self.PG)

        self.assertEqual("database", event["kind"])
        self.assertEqual(
            "FATAL: remaining connection slots are reserved for non-replication superuser connections", event["msg"]
        )

    def test_postgres_user_db_prefix_removed(self):
        event = parsers.parse_postgresql(F.PG_USER_PREFIX, self.PG)

        self.assertEqual("FATAL: sorry, too many clients already", event["msg"])

    def test_postgres_deadlock_and_panic(self):
        self.assertEqual("ERROR: deadlock detected", parsers.parse_postgresql(F.PG_DEADLOCK, self.PG)["msg"])
        self.assertIn("No space left on device", parsers.parse_postgresql(F.PG_PANIC, self.PG)["msg"])

    def test_postgres_skipped(self):
        for line in (F.PG_DETAIL, F.PG_APP_ERROR, F.PG_AUTH, F.PG_LOG):
            self.assertIsNone(parsers.parse_postgresql(line, self.PG), line)

    def test_mysql8(self):
        event = parsers.parse_mysql(F.MYSQL8_ERROR, "/var/log/mysql/error.log")

        self.assertEqual("[MY-012592] [InnoDB] Operating system error number 28 in a file operation.", event["msg"])
        self.assertIsNone(parsers.parse_mysql(F.MYSQL8_WARNING, "/var/log/mysql/error.log"))

    def test_mariadb_literals_masked_access_denied_skipped(self):
        event = parsers.parse_mysql(F.MARIADB_ERROR, "/var/log/mariadb/mariadb.log")

        self.assertEqual("mysqld: Table ? is marked as crashed and should be repaired", event["msg"])
        self.assertIsNone(parsers.parse_mysql(F.MARIADB_ACCESS, "/var/log/mariadb/mariadb.log"))

    def test_mysql_legacy_format_keeps_file_name(self):
        event = parsers.parse_mysql(F.MYSQL_LEGACY, "/var/log/mysqld.log")

        self.assertIn("Disk full (/tmp/#sql_1)", event["msg"])

    def test_mongodb(self):
        event = parsers.parse_mongodb(F.MONGO_ERROR, "/var/log/mongodb/mongod.log")

        self.assertEqual(
            "NETWORK: Error receiving request from client: SSL handshake received but server is started without SSL support",
            event["msg"],
        )
        self.assertNotIn("conn1", event["msg"])
        self.assertEqual("STORAGE: Terminating.", parsers.parse_mongodb(F.MONGO_FATAL, "/x")["msg"])
        self.assertIsNone(parsers.parse_mongodb(F.MONGO_INFO, "/x"))
        self.assertIsNone(parsers.parse_mongodb("{broken", "/x"))
        self.assertIsNone(parsers.parse_mongodb("[1,2]", "/x"))

    def test_redis(self):
        file = "/var/log/redis/redis-server.log"

        self.assertTrue(parsers.parse_redis(F.REDIS_ERROR, file)["msg"].startswith("Error moving temp DB file"))
        self.assertEqual("Background saving error", parsers.parse_redis(F.REDIS_BGSAVE, file)["msg"])
        self.assertEqual(
            "Can't save in background: fork: Cannot allocate memory", parsers.parse_redis(F.REDIS_LEGACY, file)["msg"]
        )
        self.assertIsNone(parsers.parse_redis(F.REDIS_STARTUP_WARNING, file))
        self.assertIsNone(parsers.parse_redis(F.REDIS_NOTICE, file))


class CustomParserTest(unittest.TestCase):
    def test_pattern_and_kind(self):
        parse = parsers.make_custom_parser(re.compile(r"\b(ERR|CRIT)\b"), "log_error")
        event = parse("[2026-10-21 10:00:00] ERR panel: request from 203.0.113.7 failed /admin?id=5", "/var/log/plesk/panel.log")

        self.assertEqual("log_error", event["kind"])
        self.assertEqual("ERR panel: request from [ip] failed /admin", event["msg"])
        self.assertIsNone(parse("[2026-10-21 10:00:00] INFO ok", "/var/log/plesk/panel.log"))


class LeakSweepTest(unittest.TestCase):
    def test_no_fixture_leaks_personal_data(self):
        emitted = 0

        for line, parser, file in ALL_LINES:
            event = parser(line, file)

            if event:
                emitted += 1
                self.assertIn(event["kind"], normalize.AGENT_KINDS)
                assert_clean(self, event)

        self.assertGreater(emitted, 15)

    def test_every_source_has_globs(self):
        names = [name for name, _ in parsers.FILE_SOURCES]

        self.assertEqual(
            ["nginx", "apache", "plesk", "php_fpm", "postgresql", "mysql", "mongodb", "redis"], names
        )


if __name__ == "__main__":
    unittest.main()
