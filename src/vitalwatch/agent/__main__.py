"""``python -m vitalwatch.agent`` — systemd birimi bu yolu kullanır."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
