"""wip_status.sh reports live (unmerged, content-different) branches vs the WIP cap.

Mirrors test_collision_radar.py's tmp-repo + git() helper pattern.
"""

import subprocess
from pathlib import Path

WIP_STATUS_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "guards" / "wip_status.sh"

ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**ENV, "HOME": str(repo)},
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "shared.txt").write_text("line1\n")
    (repo / "other.txt").write_text("untouched\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo


def run_wip_status(repo: Path) -> str:
    proc = subprocess.run(
        ["bash", str(WIP_STATUS_SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**ENV, "HOME": str(repo)},
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_live_branch_counted_but_squash_merged_branch_is_not(tmp_path):
    repo = make_repo(tmp_path)

    # A genuinely live branch: real, unlanded work.
    git(repo, "checkout", "-b", "claude/live", "main")
    (repo / "live.txt").write_text("still going\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "live change")
    git(repo, "checkout", "main")

    # A branch whose content already landed on main via squash-merge: it
    # keeps a commit forever "unmerged" by ancestry (main..branch stays > 0)
    # even though main now carries the same content under its own commit.
    git(repo, "checkout", "-b", "claude/done", "main")
    (repo / "other.txt").write_text("landed already\n")
    git(repo, "commit", "-am", "done change")
    git(repo, "checkout", "main")
    git(repo, "merge", "--squash", "claude/done")
    git(repo, "commit", "-m", "squash merge claude/done")

    out = run_wip_status(repo)
    assert out == "WIP: 1/5 live tracks."
