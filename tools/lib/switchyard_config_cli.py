"""CLI shim so bash guards can read one effective switchyard.toml value.

Usage:
    python3 switchyard_config_cli.py [--trusted-only] <key> <default>

Prints the effective value of SwitchyardConfig's `<key>` field (a tuple
field prints comma-joined), or prints `<default>` back out verbatim if
config loading fails for ANY reason at all - unknown key, a broken
switchyard_config import (e.g. this interpreter predates tomllib), a bug in
the loader itself. A bash guard calls this to read one value and must never
be brought down by it: the caller's own hardcoded default is the backstop,
not just load_config()'s internal one.

--trusted-only forwards to load_config(trusted_only=True): a repo-local
switchyard.toml is skipped entirely, only $SWITCHYARD_CONFIG or
~/.config/switchyard/config.toml apply. Guard-scoping keys (protected_branch,
product_remote_match) must always be read this way - see
switchyard_config.load_config's docstring for why.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    args = argv[1:]
    trusted_only = False
    if args[:1] == ["--trusted-only"]:
        trusted_only = True
        args = args[1:]

    if len(args) != 2:
        print("usage: switchyard_config_cli.py [--trusted-only] <key> <default>", file=sys.stderr)
        return 2
    key, default = args

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from switchyard_config import load_config

        cfg = load_config(trusted_only=trusted_only)
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
