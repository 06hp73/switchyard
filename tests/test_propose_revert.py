"""`switchyard propose-revert <sha>`: propose a revert PR, never land it.

Local-topology tests (a bare 'origin' + a 'station' clone, same pattern as
test_merge_train.py/test_cli.py) - the CLI's own `git fetch`/`revert`/
`push` run for real against this throwaway topology; `gh` interactions use
a stub injected via SWITCHYARD_GH (no live GitHub involved, and PATH
deliberately excludes any real `gh`).
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "train"))

CLI_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "cli.py"

ENV = {
    "PATH": "/usr/bin:/bin",  # no gh on PATH unless a test injects one via SWITCHYARD_GH
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
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=ENV, check=False
    )
    return proc.returncode == 0


def make_world(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    station = tmp_path / "station"
    subprocess.run(
        ["git", "clone", str(origin), str(station)], check=True, capture_output=True, env=ENV
    )
    (station / "app.txt").write_text("v1\n")
    git(station, "add", "-A")
    git(station, "commit", "-m", "base")
    git(station, "push", "origin", "main")
    return origin, station


def run_cli(
    *args: str, home: Path, env_extra: dict | None = None, timeout: int = 30
) -> subprocess.CompletedProcess:
    env = {**ENV, "HOME": str(home)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


# --- propose-revert: local topology, no gh --------------------------------------


def test_propose_revert_creates_branch_matching_pre_commit_tree(tmp_path):
    origin, station = make_world(tmp_path)
    before_tree = git(station, "rev-parse", "HEAD^{tree}")

    (station / "app.txt").write_text("v2 - the landed change\n")
    git(station, "commit", "-am", "landed change to revert")
    git(station, "push", "origin", "main")
    landed_sha = git(station, "rev-parse", "HEAD")

    proc = run_cli("propose-revert", landed_sha, "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    branch = f"revert-{landed_sha[:7]}"
    after_tree = git(station, "rev-parse", f"{branch}^{{tree}}")
    assert after_tree == before_tree  # content genuinely reverted, not just claimed
    assert git(origin, "rev-parse", "--verify", branch)  # pushed for real
    assert "branch pushed" in proc.stdout
    assert "gh unavailable" in proc.stdout  # no gh on PATH in ENV
    assert "never auto-readied, never auto-merged" in proc.stdout
    # station returns to a clean, known baseline on the protected branch.
    assert git(station, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_propose_revert_pr_title_uses_original_subject(tmp_path):
    origin, station = make_world(tmp_path)
    (station / "app.txt").write_text("v2\n")
    git(station, "commit", "-am", "a very specific commit subject")
    git(station, "push", "origin", "main")
    landed_sha = git(station, "rev-parse", "HEAD")

    proc = run_cli("propose-revert", landed_sha, "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    # No gh available: the fallback `gh pr create` command line is printed
    # verbatim, including the title built from the reverted commit's subject.
    assert "revert: a very specific commit subject" in proc.stdout


def test_propose_revert_unknown_sha_fails_cleanly(tmp_path):
    origin, station = make_world(tmp_path)

    proc = run_cli("propose-revert", "0" * 40, "--repo", str(station), home=tmp_path)

    assert proc.returncode == 2
    assert "could not resolve" in (proc.stdout + proc.stderr).lower()


def test_propose_revert_conflict_exits_2_and_leaves_no_branch(tmp_path):
    origin, station = make_world(tmp_path)
    (station / "app.txt").write_text("mid\n")
    git(station, "commit", "-am", "B: change to mid")
    git(station, "push", "origin", "main")
    b_sha = git(station, "rev-parse", "HEAD")
    # C overlaps B's own single-line edit, so reverting B while C is on top
    # is a guaranteed, deterministic conflict (not a race/flake).
    (station / "app.txt").write_text("new\n")
    git(station, "commit", "-am", "C: change to new, overlapping B's edit")
    git(station, "push", "origin", "main")

    proc = run_cli("propose-revert", b_sha, "--repo", str(station), home=tmp_path)

    assert proc.returncode == 2
    branch = f"revert-{b_sha[:7]}"
    assert not git_ok(station, "rev-parse", "--verify", branch)  # no local branch left
    assert not git_ok(origin, "rev-parse", "--verify", branch)  # nothing was ever pushed
    # cleaned up: no revert-in-progress state, back on a normal branch.
    assert "-- REVERTING --" not in git(station, "status")
    assert git(station, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_propose_revert_reason_file_embedded_as_fenced_data(tmp_path):
    origin, station = make_world(tmp_path)
    (station / "app.txt").write_text("v2\n")
    git(station, "commit", "-am", "landed change")
    git(station, "push", "origin", "main")
    landed_sha = git(station, "rev-parse", "HEAD")

    reason = tmp_path / "reason.txt"
    reason.write_text("pytest: test_something FAILED with AssertionError\n")

    proc = run_cli(
        "propose-revert",
        landed_sha,
        "--repo",
        str(station),
        "--reason",
        str(reason),
        home=tmp_path,
    )

    assert proc.returncode == 0, proc.stderr
    # No gh: the PR body (containing the reason file, fenced) is inside the
    # printed fallback command line.
    assert "Automated failure context" in proc.stdout
    assert "pytest: test_something FAILED" in proc.stdout
    assert "```" in proc.stdout


# --- propose-revert: gh stub -----------------------------------------------------

FAKE_GH_PR_CREATE_BODY = """# Stub `gh` for PR-create call - see write_fake_gh_pr_create() below.
import json
import os
import sys


def main():
    args = sys.argv[1:]
    argv_log = os.environ.get("FAKE_GH_ARGV_LOG")
    if argv_log:
        with open(argv_log, "a") as f:
            f.write(json.dumps(args) + "\\n")
    if args[:2] == ["pr", "create"]:
        print("https://example.invalid/pull/1")
        return 0
    sys.stderr.write("fake_gh stub: unhandled args " + repr(args) + "\\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


def write_fake_gh_pr_create(tmp_path: Path) -> Path:
    script = tmp_path / "fake_gh_pr_create.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_GH_PR_CREATE_BODY)
    script.chmod(0o755)
    return script


def test_propose_revert_gh_stub_uses_draft(tmp_path):
    origin, station = make_world(tmp_path)
    (station / "app.txt").write_text("v2\n")
    git(station, "commit", "-am", "landed change")
    git(station, "push", "origin", "main")
    landed_sha = git(station, "rev-parse", "HEAD")

    fake_gh = write_fake_gh_pr_create(tmp_path)
    argv_log = tmp_path / "argv.log"

    proc = run_cli(
        "propose-revert",
        landed_sha,
        "--repo",
        str(station),
        home=tmp_path,
        env_extra={"SWITCHYARD_GH": str(fake_gh), "FAKE_GH_ARGV_LOG": str(argv_log)},
    )

    assert proc.returncode == 0, proc.stderr
    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    create_calls = [c for c in calls if c[:2] == ["pr", "create"]]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert "--draft" in call
    assert call[call.index("--head") + 1] == f"revert-{landed_sha[:7]}"
    assert call[call.index("--base") + 1] == "main"
    assert "draft PR opened" in proc.stdout


# --- propose-revert refuses while the train lock is held (I10) --------------


def test_propose_revert_refuses_when_train_lock_held(tmp_path):
    # propose-revert mutates the SAME checkout a running train would be
    # using (fetch/checkout/revert/push) - interleaved with a live train run
    # it could corrupt that run's own in-flight state. It must take the same
    # .train/lock the train itself uses and refuse cleanly if held - held
    # for real here via the same flock _acquire_lock takes in production
    # (the lock is a kernel-managed fcntl.flock lease now; a hand-written
    # pid file on disk means nothing to it).
    from merge_train import _acquire_lock, _release_lock

    origin, station = make_world(tmp_path)
    (station / "app.txt").write_text("v2 - the landed change\n")
    git(station, "commit", "-am", "landed change to revert")
    git(station, "push", "origin", "main")
    landed_sha = git(station, "rev-parse", "HEAD")

    lock = _acquire_lock(station)
    try:
        proc = run_cli("propose-revert", landed_sha, "--repo", str(station), home=tmp_path)
    finally:
        _release_lock(lock)

    assert proc.returncode == 2
    assert "busy" in proc.stdout.lower()
    # Refused before anything was touched: no revert branch, still on main.
    branch = f"revert-{landed_sha[:7]}"
    assert not git_ok(station, "rev-parse", "--verify", branch)
    assert git(station, "rev-parse", "--abbrev-ref", "HEAD") == "main"
