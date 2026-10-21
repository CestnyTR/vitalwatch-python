"""Sürüm etiketinin kendiliğinden bulunması (kardeş paketlerle ortak kural)."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch import release  # noqa: E402

SHA = "0123456789ABCDEF0123456789abcdef01234567"
SHORT = "0123456789ab"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


class _Temp(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, relative, content):
        path = Path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        return path

    def detect(self, values=None, environ=None):
        return release.detect(root=self.root, values=values or {}, environ=environ or {})


class GitLayoutTest(_Temp):
    def test_dal_refi(self):
        self.write(".git/HEAD", "ref: refs/heads/main\n")
        self.write(".git/refs/heads/main", SHA + "\n")

        self.assertEqual((SHORT, ".git"), self.detect())

    def test_branch_name_with_slash(self):
        self.write(".git/HEAD", "ref: refs/heads/ozellik/new\n")
        self.write(".git/refs/heads/ozellik/new", SHA + "\n")

        self.assertEqual((SHORT, ".git"), self.detect())

    def test_yalnizca_packed_refs(self):
        self.write(".git/HEAD", "ref: refs/heads/main\n")
        self.write(
            ".git/packed-refs",
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{OTHER} refs/heads/baska\n"
            f"{SHA} refs/heads/main\n"
            f"^{OTHER}\n"
            f"{OTHER} refs/tags/v1.0.0\n",
        )

        self.assertEqual((SHORT, ".git"), self.detect())

    def test_empty_if_missing_from_packed_refs_too(self):
        self.write(".git/HEAD", "ref: refs/heads/main\n")
        self.write(".git/packed-refs", f"{OTHER} refs/heads/baska\n^{SHA}\n")

        self.assertEqual((None, None), self.detect())

    def test_ayrik_head(self):
        self.write(".git/HEAD", SHA + "\n")

        self.assertEqual((SHORT, ".git"), self.detect())

    def test_gitdir_dosyasi_goreli(self):
        self.write("real-git/HEAD", "ref: refs/heads/main\n")
        self.write("real-git/refs/heads/main", SHA)
        self.write("app/.git", "gitdir: ../real-git\n")

        self.assertEqual((SHORT, ".git"), release.detect(
            root=os.path.join(self.root, "app"), values={}, environ={}
        ))

    def test_gitdir_dosyasi_mutlak(self):
        self.write("real-git/HEAD", SHA)
        self.write(".git", f"gitdir: {os.path.join(self.root, 'real-git')}\n")

        self.assertEqual((SHORT, ".git"), self.detect())

    def test_broken_content(self):
        for head, ref in (
            ("ref: refs/heads/main", "broken-sha"),
            ("ref: refs/heads/main", SHA[:39]),
            ("ref: refs/heads/main", "g" * 40),
            ("anlamsiz", None),
            ("", None),
            (SHA[:12], None),
        ):
            shutil.rmtree(os.path.join(self.root, ".git"), ignore_errors=True)
            self.write(".git/HEAD", head)

            if ref is not None:
                self.write(".git/refs/heads/main", ref)

            self.assertEqual((None, None), self.detect(), (head, ref))

    def test_ref_refs_disina_cikamaz(self):
        # Kök dışındaki bir dosya sha gibi okunmasın.
        self.write("outside", SHA)
        self.write(".git/refs/heads/main", SHA)
        self.write(".git/HEAD_OTHER", SHA)

        for head in ("ref: ../disari", "ref: refs/heads/../../../disari", "ref: HEAD_BASKA",
                     "ref: refs/../HEAD_BASKA", "ref:"):
            self.write(".git/HEAD", head + "\n")

            self.assertEqual((None, None), self.detect(), head)

    def test_broken_gitdir_file(self):
        self.write(".git", "anlamsız içerik\n")
        self.assertEqual((None, None), self.detect())

        self.write(".git", "gitdir: yok/olan/yer\n")
        self.assertEqual((None, None), self.detect())

        self.write(".git", "gitdir:\n")
        self.assertEqual((None, None), self.detect())

    def test_empty_without_git(self):
        self.assertEqual((None, None), self.detect())

    def test_non_utf8_head_does_not_raise(self):
        Path(self.root, ".git").mkdir()
        Path(self.root, ".git", "HEAD").write_bytes(b"\xff\xfe\xe7")

        self.assertEqual((None, None), self.detect())

    def test_read_error_does_not_raise(self):
        self.write(".git/HEAD", SHA)

        with mock.patch("builtins.open", side_effect=PermissionError("yasak")):
            self.assertEqual((None, None), self.detect())

        with mock.patch.object(release, "_gitdir", side_effect=RuntimeError("beklenmedik")):
            self.assertEqual((None, None), self.detect())


class PrecedenceTest(_Temp):
    def setUp(self):
        super().setUp()
        self.write(".git/HEAD", SHA)

    def test_vitalwatch_release_wins_truncated_64(self):
        self.assertEqual(
            ("v2.3-canli", "VITALWATCH_RELEASE"),
            self.detect(values={"VITALWATCH_RELEASE": "  v2.3-canli \n"}, environ={"GIT_COMMIT": OTHER}),
        )
        self.assertEqual("x" * 64, self.detect(values={"VITALWATCH_RELEASE": "x" * 80})[0])
        # Olduğu gibi: sha görünümlü değer kısaltılmaz, küçültülmez.
        self.assertEqual(SHA, self.detect(values={"VITALWATCH_RELEASE": SHA})[0])

    def test_empty_vitalwatch_release_is_skipped(self):
        self.assertEqual((SHORT, ".git"), self.detect(values={"VITALWATCH_RELEASE": "   "}))

    def test_env_variable_list_matches_shared_spec(self):
        # Node ve Laravel aynı listeyi aynı sırayla okur.
        self.assertEqual(
            ("GIT_COMMIT", "GIT_SHA", "COMMIT_SHA", "SOURCE_VERSION", "VERCEL_GIT_COMMIT_SHA",
             "RENDER_GIT_COMMIT", "HEROKU_SLUG_COMMIT", "CI_COMMIT_SHA"),
            release.RELEASE_ENV_VARS,
        )

    def test_env_variable_order(self):
        environ = {name: f"{i:x}" * 40 for i, name in enumerate(release.RELEASE_ENV_VARS, start=1)}

        for name in release.RELEASE_ENV_VARS:
            value, source = self.detect(environ=environ)
            self.assertEqual(name, source)
            self.assertEqual(environ[name][:12], value)
            del environ[name]

        self.assertEqual((SHORT, ".git"), self.detect(environ=environ))

    def test_env_variable_shortened_if_hex_else_truncated(self):
        self.assertEqual(("abcdef1", "GIT_SHA"), self.detect(environ={"GIT_SHA": "ABCDEF1"}))
        self.assertEqual((SHORT, "GIT_SHA"), self.detect(environ={"GIT_SHA": SHA}))
        self.assertEqual(("sürüm-42", "COMMIT_SHA"), self.detect(environ={"COMMIT_SHA": " sürüm-42 "}))
        self.assertEqual("y" * 64, self.detect(environ={"COMMIT_SHA": "y" * 100})[0])
        # 6 hane hex eşleşmez: olduğu gibi.
        self.assertEqual(("ABCDEF", "COMMIT_SHA"), self.detect(environ={"COMMIT_SHA": "ABCDEF"}))
        # Boş değer atlanır.
        self.assertEqual(("abcdef1", "GIT_SHA"), self.detect(environ={"GIT_COMMIT": "  ", "GIT_SHA": "abcdef1"}))

    def test_default_root_is_working_directory(self):
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

        self.assertEqual((SHORT, ".git"), release.detect(values={}, environ={}))


class InitTest(_Temp):
    def setUp(self):
        super().setUp()
        import vitalwatch
        from vitalwatch import env as env_module

        self.write(".git/HEAD", SHA)
        self.previous = os.getcwd()
        self.saved = {k: os.environ.pop(k) for k in list(os.environ)
                      if k.startswith("VITALWATCH_") or k in release.RELEASE_ENV_VARS}
        self.instance = vitalwatch._instance
        os.chdir(self.root)
        env_module.forget()

        def restore():
            os.chdir(self.previous)
            os.environ.update(self.saved)
            env_module.forget()
            vitalwatch._instance = self.instance

        self.addCleanup(restore)

    def test_init_uses_git_tag(self):
        import vitalwatch

        self.assertEqual(SHORT, vitalwatch.init().release)
        self.assertEqual(SHORT, vitalwatch.from_environment()["release"])

    def test_explicit_release_wins_git_not_read(self):
        import vitalwatch

        with mock.patch.object(release, "git_sha", return_value="okunmamali") as spy:
            self.assertEqual("manual", vitalwatch.init(release="manual").release)

        self.assertEqual(0, spy.call_count)

    def test_env_beats_git(self):
        import vitalwatch

        with mock.patch.dict(os.environ, {"HEROKU_SLUG_COMMIT": OTHER}):
            self.assertEqual(OTHER[:12], vitalwatch.init().release)


if __name__ == "__main__":
    unittest.main()
