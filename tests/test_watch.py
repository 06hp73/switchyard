"""`switchyard watch install / uninstall / status`: opt-in launchd watcher.

Pure generation tests only - --dry-run prints what would happen (the plist
XML for install, the unload/remove actions for uninstall) instead of ever
touching launchctl or ~/Library/LaunchAgents. `HOME` is always redirected to
a throwaway tmp_path (same pattern test_track.py/test_cli.py already use)
as a second line of defense, so even a bug in the dry-run guard could never
reach a real machine's launchd state.
"""

import subprocess
import sys
from pathlib import Path

CLI_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "cli.py"

ENV = {
    "PATH": "/usr/bin:/bin",  # no launchctl, no gh - never touched by --dry-run anyway
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=ENV,
    )
    return proc.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=ENV
    )
    (repo / "app.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo


def write_config(repo: Path, station: Path, batch: int = 1) -> None:
    (repo / "switchyard.toml").write_text(f'[switchyard]\nstation = "{station}"\nbatch = {batch}\n')


def run_cli(*args: str, home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**ENV, "HOME": str(home)},
        timeout=timeout,
        check=False,
    )


# --- switchyard watch install --------------------------------------------------


def test_watch_install_dry_run_prints_plist_with_correct_fields(tmp_path):
    repo = make_repo(tmp_path)
    station = tmp_path / "station"
    write_config(repo, station, batch=3)

    proc = run_cli(
        "watch", "install", "--repo", str(repo), "--interval", "600", "--dry-run", home=tmp_path
    )

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"com.switchyard.{repo.name}" in out
    assert "<key>ProgramArguments</key>" in out
    assert "land" in out
    assert "--repo" in out
    assert str(station) in out
    assert "pr-squash" in out
    assert "--batch" in out
    assert "<string>3</string>" in out
    assert "<key>StartInterval</key>" in out
    assert "<integer>600</integer>" in out
    # dry-run: no real plist path, no launchctl call, nothing on disk.
    plist_path = tmp_path / "Library" / "LaunchAgents" / f"com.switchyard.{repo.name}.plist"
    assert not plist_path.exists()


def test_watch_install_default_interval_is_1200(tmp_path):
    repo = make_repo(tmp_path)
    station = tmp_path / "station"
    write_config(repo, station)

    proc = run_cli("watch", "install", "--repo", str(repo), "--dry-run", home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "<integer>1200</integer>" in proc.stdout


def test_watch_install_refuses_when_station_unset(tmp_path):
    repo = make_repo(tmp_path)
    # No switchyard.toml at all: cfg.station defaults to "".

    proc = run_cli("watch", "install", "--repo", str(repo), "--dry-run", home=tmp_path)

    assert proc.returncode != 0
    assert "station" in (proc.stdout + proc.stderr).lower()
    plist_path = tmp_path / "Library" / "LaunchAgents" / f"com.switchyard.{repo.name}.plist"
    assert not plist_path.exists()


# --- switchyard watch uninstall -------------------------------------------------


def test_watch_uninstall_dry_run_prints_actions_and_touches_nothing(tmp_path):
    repo = make_repo(tmp_path)
    plist_path = tmp_path / "Library" / "LaunchAgents" / f"com.switchyard.{repo.name}.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("pretend-existing-plist")

    proc = run_cli("watch", "uninstall", "--repo", str(repo), "--dry-run", home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "launchctl unload" in out
    assert str(plist_path) in out
    # dry-run: the (fake, pre-existing) plist must survive untouched.
    assert plist_path.exists()
    assert plist_path.read_text() == "pretend-existing-plist"


# --- switchyard watch status -----------------------------------------------------


def test_watch_status_reports_not_installed(tmp_path):
    repo = make_repo(tmp_path)

    proc = run_cli("watch", "status", "--repo", str(repo), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "not installed" in proc.stdout.lower()
