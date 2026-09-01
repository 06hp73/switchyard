"""tools/lib/resolve_python.sh: the shared Python >=3.11 interpreter resolver.

B1 (audit finding): a host whose default `python3` predates 3.11 (no
tomllib - e.g. macOS ships 3.9.6 at /usr/bin/python3) used to silently read
switchyard.toml as if it did not exist at all. config_get.sh's
`sy_cfg`/`sy_cfg_trusted` invoked a bare "${SWITCHYARD_PYTHON:-python3}" and
threw the Python-side "tomllib unavailable" warning away with `2>/dev/null`,
so nothing ever surfaced. Harmless for the shipped default config, but a
SILENT client-side protection gap for anyone who configured a custom
protected_branch / product_remote_match in a trusted config file:
git_guard.sh's main-push ban would quietly fall back to its hardcoded
defaults instead of honoring their setting - and for product_remote_match, a
hardcoded-default fallback can mean the ban disables ITSELF for the exact
repo it was configured to protect (see the git_guard.sh tests below).

This file tests three layers end to end:
  - sy_resolve_python's own resolution order (SWITCHYARD_PYTHON, versioned
    names trusted by construction, a verified bare python3), directly
    against tools/lib/resolve_python.sh.
  - bin/switchyard: exits loudly (never silently) when nothing usable is
    found, and actually runs when SWITCHYARD_PYTHON names a real
    interpreter even though every auto-detected candidate is unusable.
  - The CRITICAL fail-safe end to end: git_guard.sh, given a trusted config
    file it cannot parse, must not silently fall back to its hardcoded
    EV4SIM product match - it must warn loudly on stderr AND keep enforcing
    the main-push ban on every origin. See git_guard.sh's own comment on its
    PRODUCT_MATCH resolution for why an empty value alone cannot tell
    "unconfigured" and "unparseable" apart.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVE_SCRIPT = REPO_ROOT / "tools" / "lib" / "resolve_python.sh"
SWITCHYARD_BIN = REPO_ROOT / "bin" / "switchyard"
GUARD = REPO_ROOT / "tools" / "guards" / "git_guard.sh"

# The one real Python >=3.11 interpreter every other test in this suite
# already relies on being present on this machine (see e.g.
# test_git_guard.py's _VENV_BIN) - used here as "the real, working
# interpreter" side of each scenario.
_VENV_BIN = "/Users/storslasken/Developer/EV4XL-SIM/.venv/bin"
_VENV_PYTHON = _VENV_BIN + "/python3"

_OLD_PYTHON_STUB = """#!/usr/bin/env bash
# Simulates a pre-3.11 interpreter: fails whatever version check it is run
# with, regardless of arguments - a stand-in for e.g. macOS's real
# /usr/bin/python3 (3.9.6, no tomllib) that does not depend on the actual
# system python3's version on whatever machine runs this test.
exit 1
"""

WARNING_TEXT = (
    "switchyard: config present but unparseable (need python>=3.11); enforcing safe defaults"
)


def _make_executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _isolated_path(*extra_dirs: Path) -> str:
    """A PATH with only `extra_dirs` (checked first, in order) plus
    /usr/bin:/bin for the ordinary system tools (bash, git, jq) every guard
    script needs - deliberately excluding /opt/homebrew/bin, ~/.local/bin,
    and any other real Python install location on this machine, so
    resolution is fully determined by what each test itself places on PATH,
    never by what else happens to be installed on whatever host runs this."""
    return os.pathsep.join([str(d) for d in extra_dirs] + ["/usr/bin", "/bin"])


# --- sy_resolve_python itself ------------------------------------------------


def _resolve(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", f'source "{RESOLVE_SCRIPT}"; sy_resolve_python'],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_resolve_python_prefers_switchyard_python_override(tmp_path):
    env = {"PATH": _isolated_path(), "SWITCHYARD_PYTHON": _VENV_PYTHON}
    proc = _resolve(env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _VENV_PYTHON


def test_resolve_python_trusts_versioned_name_without_running_it(tmp_path):
    # A binary literally named python3.11 is trusted BY NAME, no runtime
    # check - so even a stub that would fail if actually executed must still
    # be picked. Proves the resolver never invokes it to "verify".
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3.11", _OLD_PYTHON_STUB)

    proc = _resolve({"PATH": _isolated_path(fake_dir)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "python3.11"


def test_resolve_python_verifies_bare_python3_before_trusting_it(tmp_path):
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)

    proc = _resolve({"PATH": _isolated_path(fake_dir)})
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""


def test_resolve_python_accepts_bare_python3_when_actually_new_enough(tmp_path):
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    (fake_dir / "python3").symlink_to(_VENV_PYTHON)

    proc = _resolve({"PATH": _isolated_path(fake_dir)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "python3"


def test_resolve_python_falls_through_when_override_is_broken(tmp_path):
    # An explicit-but-wrong SWITCHYARD_PYTHON must not leave the caller worse
    # off than not setting it at all - resolution keeps going.
    broken = _make_executable(tmp_path / "broken-override", _OLD_PYTHON_STUB)
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3.12", _OLD_PYTHON_STUB)  # trusted by name alone

    env = {"PATH": _isolated_path(fake_dir), "SWITCHYARD_PYTHON": str(broken)}
    proc = _resolve(env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "python3.12"


def test_resolve_python_returns_empty_when_nothing_usable(tmp_path):
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)

    proc = _resolve({"PATH": _isolated_path(fake_dir)})
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""


# --- bin/switchyard: hard exit, or the SWITCHYARD_PYTHON escape hatch ------


def test_bin_switchyard_exits_loudly_when_no_python_found(tmp_path):
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)

    env = {"PATH": _isolated_path(fake_dir), "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["/bin/bash", str(SWITCHYARD_BIN), "status", "--repo", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 1
    assert "no Python >=3.11 found" in proc.stderr
    assert "SWITCHYARD_PYTHON" in proc.stderr


def test_bin_switchyard_runs_when_switchyard_python_names_a_real_interpreter(tmp_path):
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)  # would fail if ever picked
    repo = tmp_path / "repo"
    repo.mkdir()

    env = {
        "PATH": _isolated_path(fake_dir),
        "HOME": str(tmp_path),
        "SWITCHYARD_PYTHON": _VENV_PYTHON,
    }
    proc = subprocess.run(
        ["/bin/bash", str(SWITCHYARD_BIN), "stats", "--repo", str(repo), "--days", "1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "no history recorded yet" in proc.stdout


# --- git_guard.sh: the CRITICAL fail-safe on an unparseable trusted config -


def _make_repo_with_origin(base: Path, name: str, origin_url: str) -> Path:
    repo = base / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", origin_url],
        check=True,
        capture_output=True,
    )
    return repo


def _run_guard(command: str, cwd: str, env: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd})
    return subprocess.run(
        ["/bin/bash", str(GUARD)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_git_guard_fails_safe_and_warns_when_trusted_config_unparseable(tmp_path):
    # A custom, real-world product_remote_match: this repo's origin would
    # match IT but does NOT match the hardcoded "06hp73/EV4SIM" fallback -
    # exactly the shape of setup that used to be silently defeated: on an
    # old-python host this config would previously vanish with no warning,
    # the guard would compare against EV4SIM instead, find no match, and
    # wave this push through.
    config = tmp_path / "config.toml"
    config.write_text('[switchyard]\nproduct_remote_match = "mycompany/ourproduct"\n')

    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)  # no >=3.11 anywhere on PATH

    repo = _make_repo_with_origin(
        tmp_path, "product", "https://github.com/mycompany/ourproduct.git"
    )

    env = {
        "PATH": _isolated_path(fake_dir),
        "HOME": str(tmp_path),
        "SWITCHYARD_CONFIG": str(config),
    }

    proc = _run_guard("git push origin main", str(repo), env)

    assert proc.returncode == 2, f"push to main must still be blocked: {proc.stdout}{proc.stderr}"
    assert "train" in proc.stderr  # the normal main-push-ban block message
    assert WARNING_TEXT in proc.stderr


def test_git_guard_same_config_honored_normally_with_a_working_interpreter(tmp_path):
    # Sanity check on the fixture itself, with a REAL >=3.11 interpreter
    # available: the custom product_remote_match is honored, and an origin
    # that does NOT match it is correctly left unenforced. This proves the
    # fail-safe test above is really exercising the "config present but
    # unparseable" path, not just "this guard always blocks everything now".
    config = tmp_path / "config.toml"
    config.write_text('[switchyard]\nproduct_remote_match = "mycompany/ourproduct"\n')

    repo = _make_repo_with_origin(tmp_path, "unrelated", "https://github.com/someoneelse/other.git")

    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "SWITCHYARD_CONFIG": str(config),
    }

    proc = _run_guard("git push origin main", str(repo), env)
    assert proc.returncode == 0, f"expected allow: {proc.stdout}{proc.stderr}"
    assert WARNING_TEXT not in proc.stderr


def test_git_guard_no_config_at_all_still_uses_ev4sim_default_not_enforce_everywhere(tmp_path):
    # Regression guard for the fix itself: with genuinely NO trusted config
    # anywhere (not "present but unparseable"), an unusable interpreter must
    # NOT flip the guard into enforcing on every origin - that would be a
    # behavior change for every unconfigured repo, including switchyard's own.
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)

    repo = _make_repo_with_origin(tmp_path, "unrelated", "https://github.com/someoneelse/other.git")

    env = {"PATH": _isolated_path(fake_dir), "HOME": str(tmp_path)}

    proc = _run_guard("git push origin main", str(repo), env)
    assert proc.returncode == 0, f"expected allow (no config at all): {proc.stdout}{proc.stderr}"
    assert WARNING_TEXT not in proc.stderr


def test_git_guard_protected_branch_falls_back_to_main_when_config_unparseable(tmp_path):
    # protected_branch's own fail-safe is simpler than product_remote_match's
    # (see config_get.sh's sy_cfg_trusted): every caller already passes
    # "main" as ITS default, so falling back to that default on an
    # unparseable config is already the safe answer, with no extra
    # disambiguation needed at the git_guard.sh call site. A config that
    # tried to rename the protected branch is still unreadable on this host,
    # so "main" is what actually gets enforced - proven here by confirming a
    # push to "main" (not the configured "trunk") is what gets blocked.
    config = tmp_path / "config.toml"
    config.write_text('[switchyard]\nprotected_branch = "trunk"\n')

    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    _make_executable(fake_dir / "python3", _OLD_PYTHON_STUB)

    repo = _make_repo_with_origin(tmp_path, "product", "https://github.com/06hp73/EV4SIM.git")

    env = {
        "PATH": _isolated_path(fake_dir),
        "HOME": str(tmp_path),
        "SWITCHYARD_CONFIG": str(config),
    }

    proc = _run_guard("git push origin main", str(repo), env)
    assert proc.returncode == 2, f"push to main must still be blocked: {proc.stdout}{proc.stderr}"
    assert WARNING_TEXT in proc.stderr
