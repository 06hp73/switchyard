"""Project config layer for the switchyard tools (switchyard.toml).

Resolution order for load_config(), first hit wins:
    1. $SWITCHYARD_CONFIG        - path to a toml file, used verbatim
    2. <git toplevel of start-or-cwd>/switchyard.toml, as the WORKING TREE
       has it
    3. that same path as the PROTECTED BRANCH has it, read with `git show
       <protected>:switchyard.toml` - used only when step 2 found no file
    4. ~/.config/switchyard/config.toml
    5. all dataclass defaults - the original hardcoded behavior, unchanged

Step 3 exists because switchyard.toml states facts about the REPO - where
its track worktrees live, which interpreter the gates run, where the station
clone is - yet keeping it in a tracked file made those facts hostage to
whichever branch happened to be checked out. A checkout parked on any branch
older than the file (a long-running feature branch, say) produced a config
of pure defaults, silently, for every key at once: the wrong interpreter, no
station, the default gates, and an empty worktree_dir that made `switchyard
track new` unusable. Reading the trunk's copy when the working tree has none
removes that coupling for good.

Step 2 still wins outright when the working tree does carry the file, so a
branch deliberately iterating on its OWN switchyard.toml is never silently
overridden by the trunk's copy. Step 3 is a floor, not an override.

load_config(trusted_only=True) skips steps 2 AND 3 entirely: only
$SWITCHYARD_CONFIG or ~/.config/switchyard/config.toml may apply, never a
repo-local file from any source. Use this for guard-scoping keys
(protected_branch, product_remote_match) that a hostile PR must never be
able to change about the guard judging it.

Reading step 3 off the trunk would in principle be safe for those keys too -
a PR author cannot write the protected branch, that being the whole point of
the merge train and the branch ruleset - but widening what the guards trust
is a security decision in its own right and is deliberately NOT bundled into
this fallback. trusted_only's boundary is exactly what it was.

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
import subprocess
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
    # process_branch's single-branch landing path, and --batch's size-1
    # bisected leaf (never a multi-member batch gate - see merge_train.py's
    # _run_gate_with_retry docstring): retry an identical failing gate once
    # before rejecting, logging a rescue to
    # .train/flaky_log.jsonl instead of landing silently. True is the
    # owner-approved default for real (CLI/config-driven) runs; run_train()
    # and process_branch() themselves default this to False so every
    # existing direct caller (including every test that predates this
    # field) keeps exercising the gate exactly once, unchanged.
    retry_flaky: bool = True
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


# Fields whose declared type is int / bool respectively - see _coerce.
# live_prefixes gets its own dedicated branch below (tuple[str, ...], not a
# plain scalar type), and every other field is a plain str.
_INT_FIELDS = frozenset({"gate_timeout", "wip_cap", "batch", "deps_age_days"})
_BOOL_FIELDS = frozenset({"retry_flaky"})


class _Invalid:
    """Sentinel returned by `_coerce` when a raw TOML value cannot be
    coerced to its field's declared type. The caller drops the key entirely
    so SwitchyardConfig's own default for that ONE field applies - a typo in
    one key must never take any other, validly-configured key down with
    it."""

    def __repr__(self) -> str:
        return "<invalid>"


_INVALID = _Invalid()


def _coerce(field: dataclasses.Field, value: object, path: Path | str) -> object:
    """Coerce `value` (already TOML-decoded) to `field`'s declared type.

    `path` is only ever interpolated into warnings, so it may equally be a
    label like "main:switchyard.toml" for config that came off the trunk
    rather than off disk.

    TOML lets an author write any scalar/array for any key regardless of
    what SwitchyardConfig declares - e.g. gate_timeout = "5400" (a quoted
    string; breaks subprocess.Popen.communicate(timeout=...) later, which
    requires a real number) or live_prefixes = "claude/" (a bare string
    instead of an array; tuple("claude/") would silently produce a
    per-CHARACTER tuple). dataclasses do not enforce their own type hints at
    construction time, so without this step a bad value flows straight into
    a frozen SwitchyardConfig unchanged. Returns `_INVALID` (after a
    warning) rather than raising - the caller must then omit the key so the
    dataclass's own default for that field applies.
    """
    name = field.name

    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        _warn(f"'{name}' in {path} must be true/false, got {value!r}; using default")
        return _INVALID

    if name in _INT_FIELDS:
        # bool is a subclass of int in Python - true/false must not sneak
        # through an int field (e.g. wip_cap = true) as 1/0.
        if isinstance(value, bool):
            _warn(f"'{name}' in {path} must be an integer, got {value!r}; using default")
            return _INVALID
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                _warn(f"'{name}' in {path} must be an integer, got {value!r}; using default")
                return _INVALID
        _warn(f"'{name}' in {path} must be an integer, got {value!r}; using default")
        return _INVALID

    if name == "live_prefixes":
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return tuple(value)
        if isinstance(value, str) and value.endswith("/"):
            _warn(
                f"'{name}' in {path} is a single string {value!r}, not a list; "
                "treating it as one prefix - wrap it in [ ... ] to silence this warning"
            )
            return (value,)
        _warn(f"'{name}' in {path} must be a list of strings, got {value!r}; using default")
        return _INVALID

    # Every remaining field (protected_branch, product_remote_match, python,
    # station, gate_fast, gate_full, priority_label, notify, worktree_dir,
    # branch_prefix) is a plain str.
    if isinstance(value, str):
        return value
    _warn(f"'{name}' in {path} must be a string, got {value!r}; using default")
    return _INVALID


def _load_from_path(path: Path) -> SwitchyardConfig:
    if tomllib is None:
        _warn(f"tomllib unavailable (needs Python 3.11+); ignoring {path}, using defaults")
        return SwitchyardConfig()

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        _warn(f"could not read {path} ({exc}); using defaults")
        return SwitchyardConfig()

    return _load_from_bytes(raw, path)


def _load_from_bytes(raw: bytes, source: Path | str) -> SwitchyardConfig:
    """Parse already-read TOML bytes into a SwitchyardConfig. Never raises.

    Split out of _load_from_path because the protected-branch fallback reads
    its bytes out of git rather than off disk, and both routes must warn,
    coerce and fail soft in exactly the same way. `source` names where the
    bytes came from, for warnings only - a Path for a real file, a label like
    "main:switchyard.toml" for a blob read out of the trunk.
    """
    if tomllib is None:
        _warn(f"tomllib unavailable (needs Python 3.11+); ignoring {source}, using defaults")
        return SwitchyardConfig()

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        _warn(f"{source} is not valid UTF-8 ({exc}); using defaults")
        return SwitchyardConfig()
    except tomllib.TOMLDecodeError as exc:
        _warn(f"malformed TOML in {source} ({exc}); using defaults")
        return SwitchyardConfig()

    table = data.get("switchyard", {})
    if not isinstance(table, dict):
        _warn(f"[switchyard] table is malformed in {source}; using defaults")
        return SwitchyardConfig()

    fields_by_name = {f.name: f for f in dataclasses.fields(SwitchyardConfig)}
    kwargs: dict[str, object] = {}
    for key, value in table.items():
        field = fields_by_name.get(key)
        if field is None:
            _warn(f"unknown config key '{key}' in {source}; ignoring")
            continue
        coerced = _coerce(field, value, source)
        if coerced is _INVALID:
            continue
        kwargs[key] = coerced

    try:
        return SwitchyardConfig(**kwargs)
    except TypeError as exc:
        _warn(f"invalid config value in {source} ({exc}); using defaults")
        return SwitchyardConfig()


def _bootstrap_protected_branch() -> str:
    """Which ref the trunk fallback reads switchyard.toml FROM.

    Resolved from trusted sources only, never from a repo-local file: asking
    the repo-local config which branch to read the repo-local config from is
    circular, and it is exactly the question the fallback exists to answer
    when that file is not there to be asked. In practice this is almost
    always the dataclass default, "main".

    No recursion risk: trusted_only=True skips the repo step entirely, so it
    can never re-enter the fallback that calls this. It is also only ever
    reached with $SWITCHYARD_CONFIG unset, since load_config returns on that
    env var long before the repo step.
    """
    return load_config(trusted_only=True).protected_branch


def _read_trunk_config_bytes(toplevel: Path, protected: str) -> bytes | None:
    """switchyard.toml as `protected` has it, or None if it has none.

    Never raises and never hangs the caller: a repo without that ref, without
    that file on it, without a usable `git` at all, or with a git that stops
    responding, simply has no trunk config - the caller then falls through to
    the home config exactly as it did before this existed. The git guards and
    the train call load_config on every single invocation and must never be
    brought down by it.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(toplevel), "show", f"{protected}:switchyard.toml"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def load_config(start: Path | None = None, *, trusted_only: bool = False) -> SwitchyardConfig:
    """Resolve and load the effective SwitchyardConfig. Never raises.

    trusted_only=True skips the repo-local file entirely, from BOTH the
    working tree and the protected branch - only $SWITCHYARD_CONFIG and
    ~/.config/switchyard/config.toml are considered, in that order, before
    falling back to defaults.

    Use trusted_only for guard-scoping decisions (git_guard.sh's
    protected_branch / product_remote_match): a repo-local switchyard.toml
    can arrive in the very same PR/branch a guard is supposed to be judging,
    so it must never be able to change what the guard protects or how it
    recognizes the protected repo. Reading it off the trunk instead would
    close that specific hole, since a PR author cannot write the protected
    branch - but widening the guards' trust boundary is its own decision and
    is deliberately left out of this fallback. Non-guard callers (the train,
    the radar, `switchyard status`/`stats`) keep trusted_only=False - a
    project's own trunk name, gate command, etc. are exactly the kind of
    thing repo-local config SHOULD be able to set for itself.
    """
    env_path = os.environ.get("SWITCHYARD_CONFIG")
    if env_path:
        path = Path(env_path)
        if path.is_file():
            return _load_from_path(path)
        _warn(f"$SWITCHYARD_CONFIG={path} does not exist; using defaults")
        return SwitchyardConfig()

    if not trusted_only:
        base = start if start is not None else Path.cwd()
        toplevel = _find_git_toplevel(base)
        if toplevel is not None:
            repo_config = toplevel / "switchyard.toml"
            if repo_config.is_file():
                return _load_from_path(repo_config)
            # The working tree has no copy - but the protected branch may.
            # This is the one case whose behavior changed: it used to fall
            # straight through to the home config (usually absent) and so to
            # pure defaults, silently, for every key at once. Warned about
            # rather than done quietly: reading config from somewhere other
            # than where the file appears to be is exactly the kind of thing
            # that must never be a surprise when a gate later runs the wrong
            # interpreter.
            protected = _bootstrap_protected_branch()
            raw = _read_trunk_config_bytes(toplevel, protected)
            if raw is not None:
                _warn(
                    f"{repo_config} is not in this working tree (the checked-out "
                    f"branch predates it, or never carried it); reading "
                    f"{protected}:switchyard.toml instead"
                )
                return _load_from_bytes(raw, f"{protected}:switchyard.toml")

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
