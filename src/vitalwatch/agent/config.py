"""Ajan yapılandırması: ``/etc/vitalwatch/agent.ini``.

Yol sırası: ``--config`` → ``VITALWATCH_AGENT_CONFIG`` → varsayılan.
``[hub]`` değerleri ortam değişkeniyle ezilir (``VITALWATCH_URL``,
``VITALWATCH_KEY``, ``VITALWATCH_SECRET``, ``VITALWATCH_ENV``,
``VITALWATCH_ENABLED``).

Ortam değişkenleri yalnızca **süreç ortamından** okunur; ``vitalwatch.env``'in
yaptığı gibi çalışma dizinindeki ``.env`` dosyasına bakılmaz. ``vitalwatch-agent
check`` bir proje dizininde çalıştırıldığında o projenin anahtarını alıp sunucu
yapılandırmasının yerine koyması yanlış teşhis olurdu.
"""

import configparser
import os
import re
from urllib.parse import urlsplit

from ..env import PREFIX
from ..reporter import normalize_env
from .normalize import AGENT_KINDS

DEFAULT_PATH = "/etc/vitalwatch/agent.ini"
CONFIG_ENV = "VITALWATCH_AGENT_CONFIG"

#: Dosya kaynakları ``parsers.FILE_SOURCES`` adlarıyla aynı; ardından süreç kaynakları.
SOURCE_NAMES = (
    "nginx", "apache", "plesk", "php_fpm", "postgresql", "mysql", "mongodb", "redis",
    "journal", "systemd", "docker", "disk",
)

_ON = ("on", "yes", "true", "1", "enabled", "evet", "acik", "açık")
_OFF = ("off", "no", "false", "0", "disabled", "hayir", "hayır", "kapali", "kapalı")


def _mode(value):
    """``auto`` / ``on`` / ``off``; tanınmayan değer ``None``."""
    text = (value or "").strip().lower()

    if text in ("", "auto", "otomatik"):
        return "auto"

    if text in _ON:
        return "on"

    if text in _OFF:
        return "off"

    return None


def _split(value, separators=r"[,\s]+"):
    return [item for item in re.split(separators, value or "") if item]


class IgnoreRules:
    """Yok sayılan host'lar ve desenler.

    Hub'ın kendi host'u her zaman listededir: hub aynı sunucudaysa hub'ın 5xx'i
    ajanın göndermesini, o gönderim de yeni hata satırını tetikleyebilirdi.
    Kontrol **ham** satırda yapılır — normalize edilmiş mesajdan ``host:``
    alanı zaten atılmıştır.
    """

    def __init__(self, hosts=(), patterns=()):
        self.hosts = sorted({host.lower().strip() for host in hosts if host and host.strip()})
        self.patterns = list(patterns)
        self._host_patterns = [
            re.compile(r"(?<![\w.-])" + re.escape(host) + r"(?![\w-]|\.[\w-])", re.IGNORECASE) for host in self.hosts
        ]

    def matches_line(self, line):
        return any(pattern.search(line) for pattern in self._host_patterns) or any(
            pattern.search(line) for pattern in self.patterns
        )

    def matches_event(self, event):
        """Site öneki (``[alan.adi] ``) yok sayılan bir host mu — Plesk'te site yoldan gelir."""
        msg = event.get("msg") or ""

        return any(msg.startswith(f"[{host}] ") for host in self.hosts) or any(
            f"/system/{host}/" in (event.get("file") or "") for host in self.hosts
        )


class AgentConfig:
    def __init__(self):
        self.path = DEFAULT_PATH
        self.file_found = False
        self.url = None
        self.key = None
        self.secret = None
        self.env_raw = "production"
        self.env = "production"
        self.enabled = True
        self.timeout = 5.0
        self.sources = {name: "auto" for name in SOURCE_NAMES}
        self.journal_errors = False
        self.docker_logs = []
        self.disk_threshold = 90
        self.ignore_hosts = []
        self.ignore_patterns = []
        self.custom_logs = []
        self.errors = []

    @property
    def hub_host(self):
        if not self.url:
            return None

        try:
            return (urlsplit(self.url).hostname or "").lower() or None
        except ValueError:
            return None

    def source_mode(self, name):
        return self.sources.get(name, "auto")

    def ignore_rules(self):
        hosts = list(self.ignore_hosts)

        if self.hub_host:
            hosts.append(self.hub_host)

        return IgnoreRules(hosts, self.ignore_patterns)


def resolve_path(path=None, environ=None):
    environ = os.environ if environ is None else environ

    return path or environ.get(CONFIG_ENV) or DEFAULT_PATH


def load(path=None, environ=None):
    """Yapılandırmayı okur. Hatalar ``config.errors``'a yazılır, fırlatılmaz."""
    environ = os.environ if environ is None else environ
    config = AgentConfig()
    config.path = resolve_path(path, environ)

    # Satır sonu yorumu yalnızca ";" — "#" düzenli ifadelerde geçebilir.
    # Interpolasyon kapalı: desenlerdeki "%" yorumlanmasın.
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",), strict=False)

    if os.path.exists(config.path):
        try:
            with open(config.path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
            config.file_found = True
        except OSError as error:
            config.errors.append(f"{config.path} okunamıyor: {error.strerror or error}")
        except (configparser.Error, UnicodeDecodeError) as error:
            config.errors.append(f"{config.path} ayrıştırılamadı: {error}")

    _apply_hub(config, parser, environ)
    _apply_sources(config, parser)
    _apply_disk(config, parser)
    _apply_ignore(config, parser)
    _apply_custom_logs(config, parser)

    return config


def _apply_hub(config, parser, environ):
    hub = parser["hub"] if parser.has_section("hub") else {}
    config.url = hub.get("url") or None
    config.key = hub.get("key") or None
    config.secret = hub.get("secret") or None
    config.env_raw = hub.get("env") or "production"
    enabled = hub.get("enabled")

    # Ortam değişkeni dosyayı ezer.
    overrides = {key: value for key, value in environ.items() if key.startswith(PREFIX) and value != ""}
    config.url = overrides.get("VITALWATCH_URL", config.url)
    config.key = overrides.get("VITALWATCH_KEY", config.key)
    config.secret = overrides.get("VITALWATCH_SECRET", config.secret)
    config.env_raw = overrides.get("VITALWATCH_ENV", config.env_raw)
    enabled = overrides.get("VITALWATCH_ENABLED", enabled)
    config.env = normalize_env(config.env_raw)
    config.enabled = str(enabled).strip().lower() != "false" if enabled is not None else True

    try:
        config.timeout = float(hub.get("timeout") or 5.0)
    except ValueError:
        config.errors.append("[hub] timeout sayı olmalı.")


def _apply_sources(config, parser):
    if not parser.has_section("sources"):
        return

    section = parser["sources"]

    for name in SOURCE_NAMES:
        if name in section:
            mode = _mode(section.get(name))

            if mode is None:
                config.errors.append(f"[sources] {name} = {section.get(name)!r}: auto, on ya da off olmalı.")
            else:
                config.sources[name] = mode

    config.journal_errors = _mode(section.get("journal_errors", "off")) == "on"
    config.docker_logs = _split(section.get("docker_logs", ""), r"[,\s]+")

    known = set(SOURCE_NAMES) | {"journal_errors", "docker_logs"}

    for name in section:
        if name not in known:
            config.errors.append(f"[sources] {name}: bilinmeyen kaynak.")


def _apply_disk(config, parser):
    if not parser.has_section("disk"):
        return

    try:
        threshold = int(parser["disk"].get("threshold", "90"))
    except ValueError:
        config.errors.append("[disk] threshold tam sayı olmalı.")
        return

    if not 50 <= threshold <= 100:
        config.errors.append("[disk] threshold 50 ile 100 arasında olmalı.")
        return

    config.disk_threshold = threshold


def _apply_ignore(config, parser):
    if not parser.has_section("ignore"):
        return

    section = parser["ignore"]
    config.ignore_hosts = _split(section.get("hosts", ""), r"[,\s]+")

    # Desenler satır satır: düzenli ifade virgül içerebilir.
    for raw in (section.get("patterns", "") or "").splitlines():
        text = raw.strip()

        if not text:
            continue

        try:
            config.ignore_patterns.append(re.compile(text))
        except re.error as error:
            config.errors.append(f"[ignore] patterns {text!r}: geçersiz düzenli ifade ({error}).")


def _apply_custom_logs(config, parser):
    for section_name in parser.sections():
        if not section_name.startswith("log:"):
            continue

        name = section_name[4:].strip() or "custom"
        section = parser[section_name]
        path = (section.get("path") or "").strip()
        pattern = (section.get("pattern") or "").strip()
        kind = (section.get("kind") or "log_error").strip()

        if not path or not pattern:
            config.errors.append(f"[{section_name}] path ve pattern zorunlu.")
            continue

        try:
            compiled = re.compile(pattern)
        except re.error as error:
            config.errors.append(f"[{section_name}] pattern geçersiz ({error}).")
            continue

        if kind not in AGENT_KINDS:
            config.errors.append(f"[{section_name}] kind {kind!r} bilinmiyor; log_error kullanılacak.")
            kind = "log_error"

        config.custom_logs.append({"name": name, "path": path, "pattern": compiled, "kind": kind})
