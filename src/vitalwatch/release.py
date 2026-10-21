"""Sürüm etiketinin (``release``) kendiliğinden bulunması.

``VITALWATCH_RELEASE`` neredeyse hiçbir kurulumda doldurulmuyor; hub bu yüzden bir
hatanın hangi deploy ile başladığını söyleyemiyordu. Deploy'lar çoğunlukla
sunucuda ``git pull`` + gunicorn yeniden başlatma; yeni süreç açılışta çalışma
dizinindeki ``.git``'i okuyup yeni commit'i kendiliğinden etiket yapar.

Kural kardeş paketlerle (Node, Laravel) birebir aynı:

1. ``VITALWATCH_RELEASE`` — boşlukları kırpılır, en çok 64 karakter, olduğu gibi.
2. CI/PaaS değişkenleri (``RELEASE_ENV_VARS`` sırasıyla) — 7–40 hane hex ise
   küçük harfle ilk 12 hane, değilse kırpılmış en çok 64 karakter.
3. Uygulama kökündeki git checkout'u: ``.git/HEAD`` → dal ref'i (yoksa
   ``packed-refs``) ya da ayrık HEAD sha'sı. Yalnızca geçerli 40 hane sha;
   küçük harfle ilk 12 hane.
4. Hiçbiri yoksa ``None``.

Hiçbir koşulda hata fırlatmaz; alt süreç (``git``) çalıştırılmaz.
"""

import os
import re

#: Sırası önemli: ilk dolu olan kazanır.
RELEASE_ENV_VARS = (
    "GIT_COMMIT",
    "GIT_SHA",
    "COMMIT_SHA",
    "SOURCE_VERSION",
    "VERCEL_GIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "HEROKU_SLUG_COMMIT",
    "CI_COMMIT_SHA",
)

MAX_LENGTH = 64
SHORT_SHA = 12

_SHORT_HEX = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)
_FULL_SHA = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)

#: Kaynak adı: ``.git`` okumasından gelen etiket.
SOURCE_GIT = ".git"


def detect(root=None, values=None, environ=None):
    """``(etiket, kaynak)`` döner; bulunamazsa ``(None, None)``.

    :param root: Uygulama kökü; verilmezse çalışma dizini.
    :param values: ``VITALWATCH_`` değerleri (``.env`` dahil); verilmezse okunur.
    :param environ: Süreç ortamı; verilmezse ``os.environ``.
    """
    try:
        if values is None:
            from .env import env as read_env

            values = read_env()

        explicit = _clean(values.get("VITALWATCH_RELEASE"))

        if explicit:
            return explicit, "VITALWATCH_RELEASE"

        environ = os.environ if environ is None else environ

        for name in RELEASE_ENV_VARS:
            value = _clean(environ.get(name))

            if value:
                if _SHORT_HEX.fullmatch(value):
                    return value.lower()[:SHORT_SHA], name

                return value, name

        sha = git_sha(root if root is not None else os.getcwd())

        if sha:
            return sha, SOURCE_GIT
    except Exception:  # noqa: BLE001
        pass

    return None, None


def git_sha(root):
    """Kökteki git checkout'unun commit'i (küçük harf, 12 hane) ya da ``None``."""
    try:
        gitdir = _gitdir(root)

        if gitdir is None:
            return None

        head = _read(os.path.join(gitdir, "HEAD"))

        if head is None:
            return None

        head = head.strip()

        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()

            # Yalnızca refs/ altı; ".." ile gitdir dışına çıkılmaz.
            if not ref.startswith("refs/") or ".." in ref:
                return None

            sha = _read(os.path.join(gitdir, *ref.split("/")))

            if sha is None:
                sha = _packed_ref(gitdir, ref)
        else:
            sha = head

        sha = (sha or "").strip()

        if _FULL_SHA.fullmatch(sha):
            return sha.lower()[:SHORT_SHA]
    except Exception:  # noqa: BLE001
        pass

    return None


def _gitdir(root):
    """``.git`` dizini; ``.git`` dosyaysa (submodule vb.) ``gitdir:`` izlenir.

    Worktree'ler (``commondir``) kapsam dışı: dal ref'i ortak dizindedir ve
    bulunamaz; orada ``VITALWATCH_RELEASE`` kullanılır (kardeş paketlerle aynı).
    """
    dotgit = os.path.join(root, ".git")

    if os.path.isdir(dotgit):
        return dotgit

    if not os.path.isfile(dotgit):
        return None

    content = _read(dotgit) or ""

    for line in content.splitlines():
        line = line.strip()

        if line.startswith("gitdir:"):
            target = line[len("gitdir:"):].strip()

            if not target:
                return None

            path = target if os.path.isabs(target) else os.path.join(root, target)

            return path if os.path.isdir(path) else None

    return None


def _packed_ref(gitdir, ref):
    # Satır satır: çok etiketli depoda packed-refs megabaytlarca olabilir.
    try:
        with open(os.path.join(gitdir, "packed-refs"), "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()

                if not line or line.startswith("#") or line.startswith("^"):
                    continue

                parts = line.split()

                if len(parts) >= 2 and parts[1] == ref:
                    return parts[0]
    except (OSError, ValueError):
        pass

    return None


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            # HEAD ve ref dosyaları küçüktür; bozuk/dev bir dosya belleği doldurmasın.
            return handle.read(1024 * 1024)
    except (OSError, ValueError):
        return None


def _clean(value):
    if value is None:
        return None

    text = str(value).strip()

    return text[:MAX_LENGTH] if text else None
