"""``vitalwatch-agent run | check [--test] | systemd``."""

import argparse
import sys
import threading

from ..client import HubClient
from ..reporter import ENVIRONMENTS, SDK_VERSION
from . import config as config_module
from .runner import DISABLED, FOUND, NOT_FOUND, Agent, source_report
from .sender import identity

#: Panelden gelen secret'in uzunluğu (``vitalwatch-status`` ile aynı kontrol).
SECRET_LENGTH = 64

TEST_MESSAGE = "vitalwatch-agent check --test ile üretilen sınama olayı"


HTTP_WARNING = "Hub adresi http:// ile başlıyor — secret imzası şifresiz hat üzerinden gider."


def hub_problems(config):
    """Hub yapılandırmasındaki sorunlar (``vitalwatch-status`` ile aynı kurallar)."""
    problems = list(config.errors)

    missing = [name for name, value in (("url", config.url), ("key", config.key), ("secret", config.secret)) if not value]

    if missing:
        problems.append(
            f"[hub] {', '.join(missing)} tanımlı değil (agent.ini ya da VITALWATCH_URL/KEY/SECRET)."
        )

    if config.secret and len(config.secret) != SECRET_LENGTH:
        problems.append(
            f"secret {SECRET_LENGTH} karakter olmalı, {len(config.secret)} karakter. Eksik kopyalanmış olabilir."
        )

    if config.url and config.url.startswith("http://"):
        problems.append(HTTP_WARNING)

    if config.env not in ENVIRONMENTS:
        problems.append(f'Ortam "{config.env_raw}" hub tarafından kabul edilmez; production, staging ya da local yazın.')

    return problems


def _line(label, value):
    print(f"  {label:<18} {value}")


def command_check(config, send_test=False):
    print("")
    _line("Ajan sürümü", SDK_VERSION)
    _line("Python", sys.version.split()[0])
    _line("Yapılandırma", f"{config.path}" + ("" if config.file_found else " (yok — yalnızca ortam değişkenleri)"))
    _line("Hub adresi", config.url or "TANIMSIZ")
    _line("Proje anahtarı", config.key or "TANIMSIZ")
    _line("Secret uzunluğu", f"{len(config.secret)} karakter" if config.secret else "TANIMSIZ")
    _line("Environment", config.env)
    _line("Etkin", "evet" if config.enabled else "HAYIR (VITALWATCH_ENABLED=false)")
    _line("Yok sayılan", ", ".join(config.ignore_rules().hosts) or "—")
    print("")
    print("  Kaynaklar:")

    rows = source_report(config)
    problems = hub_problems(config)

    for name, status, detail in rows:
        mark = "✓" if status == FOUND else ("-" if status in (DISABLED, NOT_FOUND) else "✗")
        print(f"   {mark} {name:<16} {status:<12} {detail}".rstrip())

        if status != FOUND and config.source_mode(name) == "on":
            problems.append(f"{name} kaynağı 'on' ama {status}.")

    print("")

    if not config.enabled:
        problems.append("VITALWATCH_ENABLED=false — hiçbir veri gönderilmez.")

    if problems:
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print("")

        return 1

    print("  ✓ Yapılandırma tamam.")

    if send_test:
        client = HubClient(url=config.url, key=config.key, secret=config.secret, timeout=config.timeout)
        body = dict(identity(config.env), events=[{"kind": "log_error", "msg": TEST_MESSAGE, "file": "vitalwatch-agent"}])
        result = client.send(body)

        if result.get("sent"):
            print(f"  ✓ Sınama olayı gönderildi (HTTP {result.get('status')}).")
        else:
            reason = result.get("error") or f"HTTP {result.get('status')}"
            print(f"  ✗ Sınama olayı GÖNDERİLEMEDİ: {reason}", file=sys.stderr)
            print("")

            return 1

    print("")
    print("  Verinin ulaştığı yalnızca hub panelinden doğrulanır: hub geçersiz")
    print("  imzaya da 204 döner. Sınama olayı panelde 'log_error' olarak görünmeli.")
    print("")

    return 0


def systemd_unit(python=None, config_path=None):
    """Sertleştirilmiş birim dosyası; çalışan kurulumun yorumlayıcısıyla."""
    python = python or sys.executable
    config_arg = f" --config {config_path}" if config_path and config_path != config_module.DEFAULT_PATH else ""

    # Sanal ortam /home ya da /root altındaysa ProtectHome=yes, /tmp altındaysa
    # PrivateTmp=yes yorumlayıcıyı görünmez yapar ve servis hiç açılmaz.
    notes = []
    protect_home = "yes"
    private_tmp = "yes"

    if python.startswith(("/home/", "/root/", "/run/user/")):
        protect_home = "read-only"
        notes.append("# Yorumlayıcı ev dizininde: ProtectHome=read-only. Kurulumu /opt altına\n# taşırsanız ProtectHome=yes yapın.\n")

    if python.startswith(("/tmp/", "/var/tmp/")):
        private_tmp = "no"
        notes.append("# Yorumlayıcı /tmp altında: PrivateTmp=no. Kalıcı kurulum için /opt altında bir venv önerilir.\n")

    note = "".join(notes)

    return f"""# /etc/systemd/system/vitalwatch-agent.service
# vitalwatch-agent systemd > /etc/systemd/system/vitalwatch-agent.service
# systemctl daemon-reload && systemctl enable --now vitalwatch-agent
[Unit]
Description=VitalWatch sunucu ajanı
Documentation=https://github.com/CestnyTR/vitalwatch-python
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# root: Plesk vhost logları ve Docker soketi başka türlü okunamıyor.
User=root
ExecStart={python} -m vitalwatch.agent run{config_arg}
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Nice=10
StateDirectory=vitalwatch-agent
StateDirectoryMode=0700
ProtectSystem=strict
ReadWritePaths=/var/lib/vitalwatch-agent
{note}ProtectHome={protect_home}
PrivateTmp={private_tmp}
NoNewPrivileges=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
"""


def command_run(config):
    for error in config.errors:
        print(f"vitalwatch-agent: {error}", file=sys.stderr)

    problems = [problem for problem in hub_problems(config) if problem not in config.errors]

    # http:// teşhiste hata, serviste uyarı: hub aynı makinedeyse
    # http://127.0.0.1 meşru bir kurulum.
    if HTTP_WARNING in problems:
        problems.remove(HTTP_WARNING)
        print(f"vitalwatch-agent: {HTTP_WARNING}", file=sys.stderr)

    if problems:
        for problem in problems:
            print(f"vitalwatch-agent: {problem}", file=sys.stderr)

        return 1

    if not config.enabled:
        # Servis olarak çalışırken çıkmak systemd'nin Restart=always ile
        # yeniden başlatma döngüsüne girmesi olurdu: boşta beklenir.
        print("vitalwatch-agent: VITALWATCH_ENABLED=false — hiçbir veri gönderilmez, boşta bekleniyor.", file=sys.stderr)
        import signal

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())

        while not stop.wait(3600):
            pass

        return 0

    print(f"vitalwatch-agent {SDK_VERSION}: {config.url} adresine gönderiliyor (env={config.env}).", file=sys.stderr)
    agent = Agent(config)
    agent.run()

    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="vitalwatch-agent", description="VitalWatch sunucu ajanı")
    parser.add_argument("--config", help=f"yapılandırma dosyası (varsayılan {config_module.DEFAULT_PATH})")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="servis olarak çalışır")
    check = commands.add_parser("check", help="kaynakları ve yapılandırmayı denetler")
    check.add_argument("--test", action="store_true", help="hub'a bir sınama olayı gönderir")
    commands.add_parser("systemd", help="systemd birim dosyasını yazdırır")

    # Alt komuttan sonra da --config yazılabilsin: "vitalwatch-agent run --config x".
    for sub in commands.choices.values():
        sub.add_argument("--config", dest="sub_config", help=argparse.SUPPRESS)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    config_path = getattr(args, "sub_config", None) or args.config

    if args.command == "systemd":
        print(systemd_unit(config_path=config_module.resolve_path(config_path)), end="")
        return 0

    if args.command not in ("run", "check"):
        parser.print_help()
        return 2

    config = config_module.load(config_path)

    if args.command == "check":
        return command_check(config, send_test=args.test)

    return command_run(config)


__all__ = ["main", "systemd_unit", "hub_problems", "command_check", "NOT_FOUND"]
