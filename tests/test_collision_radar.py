"""Radar detects which live branch pairs would conflict, via in-memory merge replay."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "radar"))

from collision_radar import scan  # noqa: E402  (path set up above)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
        },
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "shared.txt").write_text("line1\nline2\nline3\n")
    (repo / "other.txt").write_text("untouched\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    # claude/a edits shared.txt line1
    git(repo, "checkout", "-b", "claude/a", "main")
    (repo / "shared.txt").write_text("A-EDIT\nline2\nline3\n")
    git(repo, "commit", "-am", "a")
    # claude/b edits the SAME line -> conflicts with a, clean vs main
    git(repo, "checkout", "-b", "claude/b", "main")
    (repo / "shared.txt").write_text("B-EDIT\nline2\nline3\n")
    git(repo, "commit", "-am", "b")
    # claude/c edits a different file -> clean vs everyone
    git(repo, "checkout", "-b", "claude/c", "main")
    (repo / "other.txt").write_text("c change\n")
    git(repo, "commit", "-am", "c")
    git(repo, "checkout", "main")
    return repo


def pair(results: list[dict], a: str, b: str) -> dict:
    for r in results:
        if {r["a"], r["b"]} == {a, b}:
            return r
    raise AssertionError(f"pair {a} x {b} missing from {results}")


def test_conflicting_pair_detected(tmp_path):
    results = scan(make_repo(tmp_path))
    r = pair(results, "claude/a", "claude/b")
    assert r["clean"] is False
    assert "shared.txt" in r["files"]


def test_disjoint_pair_clean(tmp_path):
    results = scan(make_repo(tmp_path))
    assert pair(results, "claude/a", "claude/c")["clean"] is True


def test_branch_vs_main_included(tmp_path):
    results = scan(make_repo(tmp_path))
    assert pair(results, "claude/a", "main")["clean"] is True


def test_merged_branches_excluded(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-b", "claude/done", "main")
    git(repo, "checkout", "main")
    names = {r["a"] for r in scan(repo)} | {r["b"] for r in scan(repo)}
    assert "claude/done" not in names
