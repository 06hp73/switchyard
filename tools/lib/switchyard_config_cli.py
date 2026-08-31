"""CLI shim so bash guards can read one effective switchyard.toml value.

Usage:
    python3 switchyard_config_cli.py <key> <default>

Prints the effective value of SwitchyardConfig's `<key>` field (a tuple
field prints comma-joined), or prints `<default>` back out verbatim if
config loading fails for ANY reason at all - unknown key, a broken
switchyard_config import (e.g. this interpreter predates tomllib), a bug in
the loader itself. A bash guard calls this to read one value and must never
be brought down by it: the caller's own hardcoded default is the backstop,
not just load_config()'s internal one.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: switchyard_config_cli.py <key> <default>", file=sys.stderr)
        return 2
    _, key, default = argv

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from switchyard_config import load_config

        cfg = load_config()
        if not hasattr(cfg, key):
            print(default)
            return 0
        value = getattr(cfg, key)
        if isinstance(value, tuple):
            print(",".join(str(v) for v in value))
        else:
            print(value)
        return 0
    except Exception:  # noqa: BLE001 - a bash guard must never break on this
        print(default)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
