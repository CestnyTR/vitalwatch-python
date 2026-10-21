"""Ajan: normalizasyon ve maskeleme yardımcıları."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch.agent import normalize  # noqa: E402


class MaskIpsTest(unittest.TestCase):
    def test_ipv4_with_and_without_port(self):
        self.assertEqual("from [ip] and [ip]", normalize.mask_ips("from 203.0.113.7 and 198.51.100.4:8080"))

    def test_ipv6_forms(self):
        for value in ("2001:db8::1", "[2001:db8::2]:443", "fe80::1%eth0", "::1", "::ffff:192.0.2.1", "2001:db8:0:0:0:0:0:7"):
            self.assertEqual("x [ip] y", normalize.mask_ips(f"x {value} y"), value)

    def test_not_addresses(self):
        for value in ("10:00:00", "12:34", "aa:bb:cc:dd:ee:ff", "256.1.1.1", "v1.2.3.4.5", "php8.4-fpm", "error 4"):
            self.assertEqual(value, normalize.mask_ips(value), value)


class StripTest(unittest.TestCase):
    def test_query_and_fragment(self):
        self.assertEqual("http://example.com/a end", normalize.strip_query("http://example.com/a?token=1#x end"))
        self.assertEqual('"/a/b"', normalize.strip_query('"/a/b?x=1"'))
        self.assertEqual("/tmp/#sql_1", normalize.strip_query("/tmp/#sql_1"))
        self.assertEqual("what? yes", normalize.strip_query("what? yes"))

    def test_prefixes(self):
        cases = {
            "2026/10/21 10:00:00 msg": "msg",
            "[21-Oct-2026 10:00:00] msg": "msg",
            "[2026-10-21 10:00:00] ERR msg": "ERR msg",
            "2026-10-21T10:00:00.123Z msg": "msg",
            "2026-10-21 10:00:00,123 +0300 msg": "msg",
            "2026-10-21 10:00:00.123 UTC [1234] FATAL: x": "FATAL: x",
            "1234#1234: *5678 upstream": "upstream",
            "Oct 21 10:00:00 host app: msg": "app: msg",
            "[Tue Oct 21 10:00:00.123 2026] [pid 12:tid 34] msg": "msg",
        }

        for line, expected in cases.items():
            self.assertEqual(expected, normalize.strip_prefix(line), line)

    def test_clean_removes_fields(self):
        line = (
            'upstream timed out, client: 203.0.113.7, server: example.com, request: "GET /x?a=1 HTTP/1.1", '
            'upstream: "http://127.0.0.1:9000/x?a=1", host: "example.com", referrer: "https://example.com/?q=\\"1\\""'
        )

        self.assertEqual('upstream timed out, upstream: "http://[ip]/x"', normalize.clean(line))

    def test_plain_client_word_kept(self):
        self.assertEqual("Error receiving request from client: SSL", normalize.clean("Error receiving request from client: SSL"))


class RequestAndSiteTest(unittest.TestCase):
    def test_parse_request(self):
        self.assertEqual(("GET /api/x", "/api/x"), normalize.parse_request("GET /api/x?token=abc HTTP/1.1"))
        self.assertEqual(("POST /a", "/a"), normalize.parse_request("POST /a#frag HTTP/2.0"))
        self.assertEqual(("GET /p", "/p"), normalize.parse_request("GET http://example.com/p?x=1 HTTP/1.1"))
        self.assertEqual((None, None), normalize.parse_request("\\x16\\x03\\x01"))
        self.assertEqual((None, None), normalize.parse_request(None))

    def test_request_path_scrubbed(self):
        route, path = normalize.parse_request("GET /users/ali@example.com/profile HTTP/1.1")

        self.assertEqual("/users/[email]/profile", path)
        self.assertEqual("GET /users/[email]/profile", route)

    def test_site_name(self):
        self.assertEqual("example.com", normalize.site_name("Example.COM:443"))
        for value in ("_", "localhost", "203.0.113.7", "", None, "bad host", "[ip]"):
            self.assertIsNone(normalize.site_name(value), value)


class BuildEventTest(unittest.TestCase):
    def test_limit_and_prefix(self):
        event = normalize.build_event("web_server", "word " * 400, "/var/log/nginx/error.log", site="example.com")

        self.assertEqual(500, len(event["msg"]))
        self.assertTrue(event["msg"].startswith("[example.com] word word"))

    def test_scrubber_applied(self):
        event = normalize.build_event("log_error", "mail to ali@example.com phone 0532 123 45 67", "/x")

        self.assertEqual("mail to [email] phone [phone]", event["msg"])

    def test_unknown_kind_and_empty(self):
        self.assertEqual("log_error", normalize.build_event("nope", "x", "/x")["kind"])
        self.assertIsNone(normalize.build_event("log_error", "  ", "/x"))
        self.assertIsNone(normalize.build_event("log_error", ", client: 203.0.113.7", "/x"))

    def test_optional_fields(self):
        event = normalize.build_event("container_failed", "api: unhealthy", "docker", command="api", status=None)

        self.assertEqual({"kind": "container_failed", "msg": "api: unhealthy", "file": "docker", "command": "api"}, event)


if __name__ == "__main__":
    unittest.main()
