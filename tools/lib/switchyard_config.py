"""Project config layer for the switchyard tools (switchyard.toml).

Resolution order for load_config(), first hit wins:
    1. $SWITCHYARD_CONFIG        - path to a toml file, used verbatim
    2. <git toplevel of start-or-cwd>/switchyard.toml
    3. ~/.config/switchyard/config.toml
    4. all dataclass defaults - today's hardcoded behavior, unchanged

Every tool in this repo must work with zero config at all: the defaults
below are exactly today's hardcoded constants, so an absent switchyard.toml
is bit-for-bit the pre-config behavior. A broken config (missing
env-pointed file, malformed TOML, an unknown key, tomllib unavailable on
this interpreter) is a warning on stderr and a fall-back to defaults, never
an exception - the git guards and the train call this on every invocation
and must never be brought down by a typo in a config file.

Stdlib only, on purpose: this module is imported by tools that must work
with nothing installed beyond Python itself. tomllib is stdlib since
Python 3.11; on an older interpreter it is simply unavailable and config
files are silently (with a warning) treated as absent.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised only on Python < 3.11
    tomllib = None  # type: ignore[assignment]


@dataclasses.dataclass(frozen=True)
class SwitchyardConfig:
    protected_branch: str = "main"
    # Substring identifying the protected product repo's origin URL (scopes
    # git_guard.sh's main-push ban). Empty = the guard keeps its own
    # hardcoded product-repo default.
    product_remote_match: str = ""
    python: str = sys.executable  # interpreter used for gates/tools
    station: str = ""  # path to the train's station clone
    gate_fast: str = ""  # command line for the fast gate ("" = the train's GATE_DEFAULT)
    gate_full: str = ""  # command line for the full gate
    gate_timeout: int = 5400
    wip_cap: int = 5
    live_prefixes: tuple[str, ...] = ("claude/", "fix/", "feat/")
    batch: int = 1
    priority_label: str = "train-priority"
    deps_age_days: int = 7
    notify: str = "none"  # none | macos
    worktree_dir: str = ""  # where track worktrees live (track lifecycle)
    branch_prefix: str = "claude/"  # for track new


def _warn(message: str) -> None:
    print(f"switchyard config: {message}", file=sys.stderr)


def _find_git_toplevel(start: Path) -> Path | None:
    """Walk upward from `start` looking for a `.git` entry (dir or file -
    a file for worktrees/submodules). Pure filesystem walk, no `git`
    subprocess: keeps this module dependency-free and trivially testable."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _coerce(field: dataclasses.Field, value: object) -> object:
    """TOML arrays decode as lists; live_prefixes' default (and every other
    consumer's expectations, e.g. str.startswith(prefixes)) is a tuple."""
    if field.name == "live_prefixes" and isinstance(value, list):
        return tuple(value)
    return value


def _load_from_path(path: Path) -> SwitchyardConfig:
    if tomllib is None:
        _warn(f"tomllib unavailable (needs Python 3.11+); ignoring {path}, using defaults")
        return SwitchyardConfig()

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        _warn(f"could not read {path} ({exc}); using defaults")
        return SwitchyardConfig()
    except tomllib.TOMLDecodeError as exc:
        _warn(f"malformed TOML in {path} ({exc}); using defaults")
        return SwitchyardConfig()

    table = data.get("switchyard", {})
    if not isinstance(table, dict):
        _warn(f"[switchyard] table is malformed in {path}; using defaults")
        return SwitchyardConfig()

    fields_by_name = {f.name: f for f in dataclasses.fields(SwitchyardConfig)}
    kwargs: dict[str, object] = {}
    for key, value in table.items():
        field = fields_by_name.get(key)
        if field is None:
            _warn(f"unknown config key '{key}' in {path}; ignoring")
            continue
        kwargs[key] = _coerce(field, value)

    try:
        return SwitchyardConfig(**kwargs)
    except TypeError as exc:
        _warn(f"invalid config value in {path} ({exc}); using defaults")
        return SwitchyardConfig()


def load_config(start: Path | None = None) -> SwitchyardConfig:
    """Resolve and load the effective SwitchyardConfig. Never raises."""
    env_path = os.environ.get("SWITCHYARD_CONFIG")
    if env_path:
        path = Path(env_path)
        if path.is_file():
            return _load_from_path(path)
        _warn(f"$SWITCHYARD_CONFIG={path} does not exist; using defaults")
        return SwitchyardConfig()

    base = start if start is not None else Path.cwd()
    toplevel = _find_git_toplevel(base)
    if toplevel is not None:
        repo_config = toplevel / "switchyard.toml"
        if repo_config.is_file():
            return _load_from_path(repo_config)

    home_config = Path.home() / ".config" / "switchyard" / "config.toml"
    if home_config.is_file():
        return _load_from_path(home_config)

    return SwitchyardConfig()


def dump_effective(cfg: SwitchyardConfig) -> str:
    """Render every field of `cfg` as `key = value`, one per line.

    For the future `switchyard status`-style command - a human-readable
    dump of what actually resolved, wherever it came from.
    """
    return "\n".join(f"{f.name} = {getattr(cfg, f.name)!r}" for f in dataclasses.fields(cfg))
