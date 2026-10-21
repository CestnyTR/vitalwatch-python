"""Ajan: dosya okuyucu — sondan başlama, döndürme, kırpılma, durumdan devam."""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch.agent import state as state_module  # noqa: E402
from vitalwatch.agent import tail  # noqa: E402


def parser(line, file):
    return line


class TailTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "error.log")
        self.warnings = []

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def write(self, text, path=None, mode="a"):
        with open(path or self.path, mode, encoding="utf-8") as handle:
            handle.write(text)

    def tailer(self, saved=None, pattern="*.log"):
        instance = tail.Tailer([(os.path.join(self.directory, pattern), parser, "test")], saved=saved, warn=self.warnings.append)
        instance.scan()

        return instance

    def lines(self, tailer):
        return [line for _, line, _ in tailer.poll()]

    def test_first_sight_starts_at_end(self):
        self.write("old 1\nold 2\n")
        tailer = self.tailer()

        self.assertEqual([], self.lines(tailer))

        self.write("new 1\nnew 2\n")
        self.assertEqual(["new 1", "new 2"], self.lines(tailer))
        self.assertEqual([], self.lines(tailer))

    def test_partial_line_waits_for_newline(self):
        self.write("")
        tailer = self.tailer()
        self.write("half")

        self.assertEqual([], self.lines(tailer))

        self.write(" done\n")
        self.assertEqual(["half done"], self.lines(tailer))

    def test_rotation_by_rename(self):
        self.write("a\n")
        tailer = self.tailer()
        self.write("b\n")
        self.assertEqual(["b"], self.lines(tailer))

        os.rename(self.path, self.path + ".1")
        self.write("c\n", mode="w")

        self.assertEqual(["c"], self.lines(tailer))

    def test_truncation(self):
        self.write("a long first line\n")
        tailer = self.tailer()
        self.write("second\n")
        self.assertEqual(["second"], self.lines(tailer))

        self.write("x\n", mode="w")  # copytruncate sonrası kısa içerik

        self.assertEqual(["x"], self.lines(tailer))

    def test_state_resume(self):
        self.write("a\n")
        tailer = self.tailer()
        self.write("b\n")
        self.assertEqual(["b"], self.lines(tailer))
        saved = tailer.state()

        # Ajan kapalıyken yazılanlar yeniden açılışta okunur.
        self.write("c\nd\n")
        resumed = self.tailer(saved=saved)

        self.assertEqual(["c", "d"], self.lines(resumed))

    def test_state_resume_after_rotation_while_down(self):
        self.write("a\n")
        saved = self.tailer().state()

        os.rename(self.path, self.path + ".1")
        self.write("fresh\n", mode="w")

        self.assertEqual(["fresh"], self.lines(self.tailer(saved=saved)))

    def test_state_store_roundtrip(self):
        self.write("a\n")
        tailer = self.tailer()
        store = state_module.StateStore(os.path.join(self.directory, "state"))

        self.assertTrue(store.save({"files": tailer.state(), "journal_cursor": "s=1"}))
        self.assertEqual(tailer.state(), store.load()["files"])
        self.assertEqual("s=1", store.load()["journal_cursor"])
        self.assertEqual(0o600, os.stat(store.path).st_mode & 0o777)
        self.assertEqual(["state.json"], os.listdir(store.directory))

    def test_state_store_corrupt_file(self):
        store = state_module.StateStore(os.path.join(self.directory, "state"))
        self.write("{broken", path=store.path, mode="w")

        self.assertEqual({}, store.load())

    def test_state_store_falls_back_when_not_writable(self):
        blocker = os.path.join(self.directory, "file")
        self.write("x", path=blocker, mode="w")

        with unittest.mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.directory, "xdg")}), \
                unittest.mock.patch("sys.stderr"):
            store = state_module.StateStore(os.path.join(blocker, "sub"))

        self.assertEqual(os.path.join(self.directory, "xdg", "vitalwatch-agent"), store.directory)
        self.assertTrue(store.save({"a": 1}))

    def test_new_file_after_first_scan_read_from_start(self):
        self.write("a\n")
        tailer = self.tailer()
        other = os.path.join(self.directory, "site-error.log")
        self.write("first\n", path=other)

        tailer.scan()

        self.assertEqual(["first"], self.lines(tailer))

    def test_rotated_names_not_followed(self):
        for name in ("error.log.1", "error.log.2.gz", "error_log-20261021", "error.log-2026102100"):
            self.write("x\n", path=os.path.join(self.directory, name))

        tailer = self.tailer(pattern="*error*")

        self.assertEqual([], tailer.files())

    def test_disappearing_file_dropped_on_rescan(self):
        self.write("a\n")
        tailer = self.tailer()
        os.unlink(self.path)

        self.assertEqual([], self.lines(tailer))
        tailer.scan()
        self.assertEqual({}, tailer.state())

    def test_unreadable_file_warned_once(self):
        if os.geteuid() == 0:
            self.skipTest("root her dosyayı okur")

        self.write("a\n")
        os.chmod(self.path, 0)

        tailer = self.tailer()
        tailer.scan()

        self.assertEqual([], tailer.files())
        self.assertEqual(1, len(self.warnings))
        self.assertIn("okunamıyor", self.warnings[0])

    def test_invalid_utf8_replaced(self):
        self.write("")
        tailer = self.tailer()

        with open(self.path, "ab") as handle:
            handle.write(b"bad \xff byte\n")

        self.assertEqual(["bad � byte"], self.lines(tailer))

if __name__ == "__main__":
    unittest.main()
