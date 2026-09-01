"""gate.sh / gate_full.sh: the interpreter they run under ($PY) must honor
SWITCHYARD_PYTHON / switchyard.toml's `python` key, not a hardcoded absolute
path to one machine's venv - see tools/lib/switchyard_config.py's `python`
field and config_get.sh's sy_cfg.

These tests run the REAL scripts end to end (not a hand-copied excerpt), but
substitute a fake "python" shim for $PY so nothing here actually invokes
ruff or the pytest suite - a long, heavy, and here pointless run just to
prove which interpreter path was selected. The shim logs its own argv[0]
and exits 0 immediately at every invocation (the import-preflight heredoc,
then ruff, then one or two pytest invocations), which satisfies `set -eu`
at each step so the whole script completes in well under a second.
"""

import os
import stat
import subprocess
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parents[1] / "tools" / "train"
GATE_SCRIPTS = [GATE_DIR / "gate.sh", GATE_DIR / "gate_full.sh"]

# The EV4XL-SIM venv is the only Python 3.11+ interpreter available in this
# environment (tomllib requires it); config_get.sh's sy_cfg shells out to
# "${SWITCHYARD_PYTHON:-python3}" to READ the config file itself, which is a
# separate concern from what value that config file's `python` key holds -
# tests that need sy_cfg to actually parse a switchyard.toml put this on
# PATH so that inner read succeeds, regardless of what fake path the TOML
# itself then names for gate.sh's own $PY.
_VENV_BIN = "/Users/storslasken/Developer/EV4XL-SIM/.venv/bin"

_FAKE_PY = """#!/usr/bin/env bash
# Fake interpreter for tests: log our own argv[0], drain stdin (the import
# preflight is piped in via heredoc), always succeed.
echo "$0" >> "$FAKE_PY_LOG"
cat >/dev/null
exit 0
"""


def _make_fake_python(path: Path) -> Path:
    path.write_text(_FAKE_PY)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_gate(script: Path, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=15,
        check=False,
    )


def test_gate_scripts_pass_bash_syntax_check():
    for script in GATE_SCRIPTS:
        proc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10, check=False
        )
        assert proc.returncode == 0, f"{script}: {proc.stderr}"


def test_gate_scripts_honor_switchyard_python_env_var(tmp_path):
    for i, script in enumerate(GATE_SCRIPTS):
        fake_py = _make_fake_python(tmp_path / f"fake-python-env-{i}")
        log = tmp_path / f"log-env-{i}.txt"
        env = {**os.environ, "SWITCHYARD_PYTHON": str(fake_py), "FAKE_PY_LOG": str(log)}
        env.pop("SWITCHYARD_CONFIG", None)

        proc = _run_gate(script, env, tmp_path)

        assert proc.returncode == 0, f"{script}: {proc.stderr}"
        assert log.exists(), f"{script}: fake interpreter never ran"
        calls = log.read_text().splitlines()
        assert calls and all(c == str(fake_py) for c in calls), f"{script}: {calls}"


def test_gate_scripts_default_to_python3_when_nothing_configured(tmp_path):
    # No SWITCHYARD_PYTHON, no config anywhere (isolated HOME, no
    # SWITCHYARD_CONFIG) - falls back to a bare "python3", resolved via a
    # fake shim placed at the front of PATH under that exact name.
    for i, script in enumerate(GATE_SCRIPTS):
        bin_dir = tmp_path / f"bin-default-{i}"
        bin_dir.mkdir()
        _make_fake_python(bin_dir / "python3")
        log = tmp_path / f"log-default-{i}.txt"
        isolated_home = tmp_path / f"home-default-{i}"
        isolated_home.mkdir()
        env = {
            "PATH": str(bin_dir) + os.pathsep + "/usr/bin:/bin",
            "HOME": str(isolated_home),
            "FAKE_PY_LOG": str(log),
        }

        proc = _run_gate(script, env, tmp_path)

        assert proc.returncode == 0, f"{script}: {proc.stderr}"
        assert log.exists(), f"{script}: fake interpreter never ran"


def test_gate_scripts_honor_configured_python(tmp_path):
    for i, script in enumerate(GATE_SCRIPTS):
        fake_py = _make_fake_python(tmp_path / f"fake-python-cfg-{i}")
        config = tmp_path / f"cfg-{i}.toml"
        config.write_text(f'[switchyard]\npython = "{fake_py}"\n')
        log = tmp_path / f"log-cfg-{i}.txt"
        env = {
            **os.environ,
            "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
            "SWITCHYARD_CONFIG": str(config),
            "FAKE_PY_LOG": str(log),
        }
        env.pop("SWITCHYARD_PYTHON", None)

        proc = _run_gate(script, env, tmp_path)

        assert proc.returncode == 0, f"{script}: {proc.stderr}"
        assert log.exists(), f"{script}: fake interpreter never ran"
        calls = log.read_text().splitlines()
        assert calls and all(c == str(fake_py) for c in calls), f"{script}: {calls}"


def test_gate_scripts_env_var_wins_over_configured_python(tmp_path):
    for i, script in enumerate(GATE_SCRIPTS):
        fake_cfg_py = _make_fake_python(tmp_path / f"fake-python-cfg-win-{i}")
        fake_env_py = _make_fake_python(tmp_path / f"fake-python-env-win-{i}")
        config = tmp_path / f"cfg-win-{i}.toml"
        config.write_text(f'[switchyard]\npython = "{fake_cfg_py}"\n')
        log = tmp_path / f"log-win-{i}.txt"
        env = {
            **os.environ,
            "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
            "SWITCHYARD_CONFIG": str(config),
            "SWITCHYARD_PYTHON": str(fake_env_py),
            "FAKE_PY_LOG": str(log),
        }

        proc = _run_gate(script, env, tmp_path)

        assert proc.returncode == 0, f"{script}: {proc.stderr}"
        calls = log.read_text().splitlines()
        assert calls and all(c == str(fake_env_py) for c in calls), (
            f"{script}: expected {fake_env_py}, got {calls}"
        )
