"""Track lifecycle: `switchyard track new` / `switchyard track done`.

Local-topology tests only (no `gh` on PATH, matching the printed-fallback
path both subcommands document for a gh-less environment): `track new` must
still create the branch + worktree and push it, and `track done` needs
`--force-local` to skip the "PR is MERGED" check gh would otherwise perform.
"""

import subprocess
import sys
import time
from pathlib import Path

CLI_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "cli.py"

ENV = {
    "PATH": "/usr/bin:/bin",  # deliberately excludes /opt/homebrew/bin: no `gh` on PATH
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


def git_ok(cwd: Path, *args: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=ENV,
        check=False,
    )
    return proc.returncode == 0


def make_world(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True, env=ENV
    )
    (repo / "app.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "main")
    return origin, repo


def write_config(repo: Path, worktree_root: Path, branch_prefix: str = "claude/") -> None:
    (repo / "switchyard.toml").write_text(
        f'[switchyard]\nworktree_dir = "{worktree_root}"\nbranch_prefix = "{branch_prefix}"\n'
    )


def run_cli(*args: str, home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**ENV, "HOME": str(home)},
        timeout=timeout,
        check=False,
    )


# --- switchyard track new ------------------------------------------------------


def test_track_new_creates_branch_and_worktree_with_gh_fallback(tmp_path):
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)

    proc = run_cli("track", "new", "shiny-feature", "--repo", str(repo), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    # gh is not on PATH: track new must still do everything local and say so,
    # not fail the whole command.
    assert "gh" in proc.stdout.lower()

    branch = "claude/shiny-feature"
    wt_path = worktree_root / "shiny-feature"
    assert wt_path.is_dir()
    assert (wt_path / ".git").exists()
    assert git(repo, "rev-parse", "--verify", branch)
    assert git(wt_path, "rev-parse", "--abbrev-ref", "HEAD") == branch
    # pushed with -u
    assert git(origin, "rev-parse", "--verify", branch)
    assert git(origin, "rev-parse", branch) == git(repo, "rev-parse", branch)
    # the worktree path is surfaced to the user
    assert str(wt_path) in proc.stdout


def test_track_new_errors_clearly_when_worktree_dir_unset(tmp_path):
    origin, repo = make_world(tmp_path)
    # No switchyard.toml at all: cfg.worktree_dir defaults to "".

    proc = run_cli("track", "new", "no-config", "--repo", str(repo), home=tmp_path)

    assert proc.returncode != 0
    assert "worktree_dir" in (proc.stdout + proc.stderr)
    # Nothing was created.
    assert not git_ok(repo, "rev-parse", "--verify", "claude/no-config")


# --- switchyard track done ------------------------------------------------------


def test_track_done_force_local_removes_worktree_and_branches(tmp_path):
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    new = run_cli("track", "new", "feat-x", "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr
    wt_path = worktree_root / "feat-x"
    assert wt_path.is_dir()

    done = run_cli("track", "done", "feat-x", "--repo", str(repo), "--force-local", home=tmp_path)

    assert done.returncode == 0, done.stderr
    assert not wt_path.exists()
    assert not git_ok(repo, "rev-parse", "--verify", "claude/feat-x")
    assert not git_ok(origin, "rev-parse", "--verify", "claude/feat-x")


def test_track_done_refuses_dirty_worktree(tmp_path):
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    new = run_cli("track", "new", "dirty-one", "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr
    wt_path = worktree_root / "dirty-one"
    (wt_path / "uncommitted.txt").write_text("oops\n")

    done = run_cli(
        "track", "done", "dirty-one", "--repo", str(repo), "--force-local", home=tmp_path
    )

    assert done.returncode != 0
    # Refused before touching anything: worktree, file, and both branches survive.
    assert wt_path.is_dir()
    assert (wt_path / "uncommitted.txt").exists()
    assert git(repo, "rev-parse", "--verify", "claude/dirty-one")
    assert git(origin, "rev-parse", "--verify", "claude/dirty-one")


def test_track_done_dry_run_leaves_everything(tmp_path):
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    new = run_cli("track", "new", "keep-me", "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr
    wt_path = worktree_root / "keep-me"

    done = run_cli(
        "track",
        "done",
        "keep-me",
        "--repo",
        str(repo),
        "--force-local",
        "--dry-run",
        home=tmp_path,
    )

    assert done.returncode == 0, done.stderr
    assert "dry" in done.stdout.lower()
    assert wt_path.is_dir()
    assert git(repo, "rev-parse", "--verify", "claude/keep-me")
    assert git(origin, "rev-parse", "--verify", "claude/keep-me")


def test_track_done_without_force_local_requires_merged_pr(tmp_path):
    # No --force-local and no gh on PATH: must refuse rather than silently
    # trust an unverified branch, and must not touch anything.
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    new = run_cli("track", "new", "unverified", "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr
    wt_path = worktree_root / "unverified"

    done = run_cli("track", "done", "unverified", "--repo", str(repo), home=tmp_path)

    assert done.returncode != 0
    assert wt_path.is_dir()
    assert git(repo, "rev-parse", "--verify", "claude/unverified")


def test_track_new_and_done_survive_a_full_round_trip(tmp_path):
    # Not one of the four explicitly-required scenarios, but cheap insurance
    # that new -> done composes cleanly end to end with a name containing a
    # timestamp-like suffix, closer to how a real session would name a track.
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root, branch_prefix="claude/")
    name = f"round-trip-{int(time.time())}"

    new = run_cli("track", "new", name, "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr

    done = run_cli("track", "done", name, "--repo", str(repo), "--force-local", home=tmp_path)
    assert done.returncode == 0, done.stderr
    assert not (worktree_root / name).exists()
