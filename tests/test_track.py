"""Track lifecycle: `switchyard track new` / `switchyard track done`.

Local-topology tests only (no `gh` on PATH, matching the printed-fallback
path both subcommands document for a gh-less environment): `track new` must
still create the branch + worktree and push it, and `track done` needs
`--force-local` to skip the "PR is MERGED" check gh would otherwise perform.
"""

import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "train"))

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


def run_cli(
    *args: str, home: Path, timeout: int = 30, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**ENV, "HOME": str(home), **(extra_env or {})},
        timeout=timeout,
        check=False,
    )


def write_stub_gh(tmp_path: Path, exit_code: int, stderr: str = "", stdout: str = "") -> Path:
    """A fake `gh` for SWITCHYARD_GH, so the draft-PR half of `track new` is
    exercised at all. Every other test in this file runs with no gh on PATH
    and therefore only ever reaches the printed-fallback path - which is how
    a PR-creation bug (GitHub refusing a head branch with no commits of its
    own) survived unnoticed: nothing here ever called gh."""
    stub = tmp_path / "stub-gh"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s' {shlex.quote(stdout)}\n"
        f"printf '%s' {shlex.quote(stderr)} >&2\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)
    return stub


def write_github_like_gh(tmp_path: Path) -> Path:
    """A `gh pr create` stub that enforces the one GitHub rule this command
    kept tripping over: createPullRequest refuses a head branch carrying no
    commits the base does not already have. A stub that always succeeds
    would pass against the very bug these tests exist to catch."""
    stub = tmp_path / "github-like-gh"
    stub.write_text(
        "#!/bin/sh\n"
        'head=""; base=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    --head) head="$2"; shift 2;;\n'
        '    --base) base="$2"; shift 2;;\n'
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        'n=$(git rev-list --count "$base".."$head" 2>/dev/null || echo 0)\n'
        'if [ "${n:-0}" -eq 0 ]; then\n'
        '  echo "pull request create failed: GraphQL: No commits between '
        '$base and $head (createPullRequest)" >&2\n'
        "  exit 1\n"
        "fi\n"
        "echo https://example.invalid/pr/1\n"
    )
    stub.chmod(0o755)
    return stub


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


def test_track_new_seeds_a_commit_so_a_draft_pr_can_open(tmp_path):
    # GitHub refuses createPullRequest with "No commits between <base> and
    # <head>", so a track branch that is level with the protected branch can
    # never have a PR opened for it. track new must therefore return with the
    # branch already one commit ahead...
    origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)

    proc = run_cli("track", "new", "seeded", "--repo", str(repo), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    branch = "claude/seeded"
    assert int(git(repo, "rev-list", "--count", f"main..{branch}")) == 1
    # ...and level in CONTENT, so wip_status.sh's tip-vs-tip diff keeps a
    # freshly-opened track out of the live WIP count until real work lands.
    assert git_ok(repo, "diff", "--quiet", "main", branch)
    # The seed commit is pushed too - GitHub judges the remote head, not the
    # local one.
    assert git(origin, "rev-parse", branch) == git(repo, "rev-parse", branch)


def test_track_new_opens_the_draft_pr_when_gh_succeeds(tmp_path):
    _origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    stub = write_github_like_gh(tmp_path)

    proc = run_cli(
        "track",
        "new",
        "with-pr",
        "--repo",
        str(repo),
        home=tmp_path,
        extra_env={"SWITCHYARD_GH": str(stub)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "draft PR opened." in proc.stdout


def test_track_new_reports_ghs_own_reason_when_pr_creation_fails(tmp_path):
    # The old blanket "gh unavailable or PR creation failed" hid the real
    # cause behind a wrong guess about PATH; gh's stderr must reach the user.
    _origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    stub = write_stub_gh(
        tmp_path,
        exit_code=1,
        stderr="pull request create failed: GraphQL: No commits between main and claude/x\n",
    )

    proc = run_cli(
        "track",
        "new",
        "no-pr",
        "--repo",
        str(repo),
        home=tmp_path,
        extra_env={"SWITCHYARD_GH": str(stub)},
    )

    # Still a success overall: branch and worktree are real either way.
    assert proc.returncode == 0, proc.stderr
    assert "No commits between main and claude/x" in proc.stdout
    assert (worktree_root / "no-pr").is_dir()


def test_track_new_errors_clearly_when_worktree_dir_unset(tmp_path):
    _origin, repo = make_world(tmp_path)
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
    _origin, repo = make_world(tmp_path)
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
    _origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root, branch_prefix="claude/")
    name = f"round-trip-{int(time.time())}"

    new = run_cli("track", "new", name, "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr

    done = run_cli("track", "done", name, "--repo", str(repo), "--force-local", home=tmp_path)
    assert done.returncode == 0, done.stderr
    assert not (worktree_root / name).exists()


# --- track done refuses while the train lock is held (I10) ------------------


def test_track_done_refuses_when_train_lock_held(tmp_path):
    # track done deletes local/remote branches on the SAME repo a running
    # train would be checking out branches into - interleaved with a live
    # train run it could delete a branch mid-merge or corrupt its checkout.
    # It must take the same .train/lock the train itself uses and refuse
    # cleanly if held - held for real here via the same flock _acquire_lock
    # takes in production (the lock is a kernel-managed fcntl.flock lease
    # now; a hand-written pid file on disk means nothing to it).
    from merge_train import _acquire_lock, _release_lock

    _origin, repo = make_world(tmp_path)
    worktree_root = tmp_path / "worktrees"
    write_config(repo, worktree_root)
    new = run_cli("track", "new", "locked-out", "--repo", str(repo), home=tmp_path)
    assert new.returncode == 0, new.stderr
    wt_path = worktree_root / "locked-out"

    lock = _acquire_lock(repo)
    try:
        done = run_cli(
            "track", "done", "locked-out", "--repo", str(repo), "--force-local", home=tmp_path
        )
    finally:
        _release_lock(lock)

    assert done.returncode == 2
    assert "busy" in done.stdout.lower()
    # Refused before anything was touched.
    assert wt_path.is_dir()
    assert git(repo, "rev-parse", "--verify", "claude/locked-out")
