"""The guard script blocks git operations that are unsafe with parallel sessions.

Contract: stdin = PreToolUse JSON; exit 0 = allow, exit 2 = block (reason on
stderr). The script must never block on parse errors (fail-open, exit 0):
a broken guard must not paralyze every session.
"""

import json
import subprocess
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "tools" / "guards" / "git_guard.sh"


def run_guard(command: str, cwd: str = "/tmp") -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd})
    return subprocess.run(
        ["bash", str(GUARD)], input=payload, capture_output=True, text=True, timeout=10
    )


def assert_blocked(command: str, needle: str, cwd: str = "/tmp") -> None:
    result = run_guard(command, cwd=cwd)
    assert result.returncode == 2, f"expected block for: {command}"
    assert needle in result.stderr


def assert_allowed(command: str, cwd: str = "/tmp") -> None:
    result = run_guard(command, cwd=cwd)
    assert result.returncode == 0, f"expected allow for: {command}\nstderr: {result.stderr}"


def make_repo_with_origin(base: Path, name: str, origin_url: str | None) -> Path:
    """Create a real tmp git repo (optionally with an 'origin' remote) to use as cwd."""
    repo = base / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    if origin_url is not None:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", origin_url],
            check=True,
            capture_output=True,
        )
    return repo


def test_blocks_git_stash():
    assert_blocked("git stash", "stash")
    assert_blocked("rtk git stash pop", "stash")
    assert_blocked("cd /x && git stash push -u -m tag", "stash")
    assert_blocked("git  stash", "stash")


def test_allows_stash_list():
    assert_allowed("git stash list")
    assert_allowed("rtk git stash list --format='%H'")


def test_blocks_identity_change():
    assert_blocked("git config user.email evil@example.com", "identity")
    assert_blocked("git config --global user.name Bot", "identity")
    assert_blocked("git -c user.email=evil@example.com commit -m x", "identity")
    assert_blocked("git -c user.name=Bot commit", "identity")
    assert_blocked("git commit --author='Evil <e@x.com>' -m x", "identity")
    assert_blocked("GIT_AUTHOR_EMAIL=e@x.com git commit -m x", "identity")
    assert_blocked("GIT_COMMITTER_NAME=Bot git commit -m x", "identity")


def test_blocks_force_push():
    assert_blocked("git push --force origin claude/x", "force")
    assert_blocked("git push -f", "force")
    assert_blocked("git push --force-with-lease", "force")
    assert_blocked("git push origin +claude/x:claude/x", "force")


def test_blocks_direct_push_to_main():
    assert_blocked("git push origin main", "train")
    assert_blocked("rtk git push -u origin main", "train")
    assert_blocked("git push origin HEAD:main", "train")
    assert_blocked("git push origin refs/heads/main", "train")
    assert_blocked("git push origin main --tags", "train")
    assert_blocked("git push origin fix-branch:main", "train")
    assert_blocked("git push origin :main", "train")
    assert_blocked("git push origin HEAD:refs/heads/main", "train")


def test_main_push_ban_scoped_to_product_repo_origin(tmp_path):
    # The rule only applies when cwd's origin is the product repo (06hp73/EV4SIM).
    product_repo = make_repo_with_origin(
        tmp_path, "product", "https://github.com/06hp73/EV4SIM.git"
    )
    assert_blocked("git push origin main", "train", cwd=str(product_repo))

    # switchyard's own main is a different repo entirely - pushing it is fine.
    switchyard_repo = make_repo_with_origin(
        tmp_path, "switchyard", "https://github.com/06hp73/switchyard.git"
    )
    assert_allowed("git push origin main", cwd=str(switchyard_repo))

    # ssh remote form must match too - not just the https form.
    ssh_repo = make_repo_with_origin(tmp_path, "ssh-clone", "git@github.com:06hp73/EV4SIM.git")
    assert_blocked("git push origin main", "train", cwd=str(ssh_repo))


def test_main_push_ban_fail_safe_when_origin_undeterminable(tmp_path):
    # A real git repo with no 'origin' remote at all: the rule cannot determine
    # the repo's identity, so it must stay enforced (fail-safe toward protection).
    no_origin_repo = tmp_path / "no-origin"
    no_origin_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(no_origin_repo)], check=True, capture_output=True)
    assert_blocked("git push origin main", "train", cwd=str(no_origin_repo))

    # A cwd that plain doesn't exist at all is equally undeterminable.
    assert_blocked("git push origin main", "train", cwd=str(tmp_path / "does-not-exist"))


def test_allows_feature_branch_push():
    assert_allowed("git push")
    assert_allowed("git push -u origin claude/parallel-fix-collisions-b2d1f3")
    assert_allowed("rtk git push origin fix/fcr-export-adequacy")
    assert_allowed("git push origin main-feature")
    assert_allowed("git push origin feature-main")
    assert_allowed("git push origin feature/main-fix")
    assert_allowed("git push origin claude/a:claude/a")
    assert_allowed("git push origin HEAD:claude/refresh")


def test_blocks_rm_rf_on_worktrees():
    assert_blocked("rm -rf .claude/worktrees/foo", "worktree remove")
    assert_blocked("rm -rf /Users/x/repo/.claude/worktrees", "worktree remove")


def test_allows_ordinary_commands():
    assert_allowed("git status")
    assert_allowed("git commit -m 'feat: x'")
    assert_allowed("ls -la && git diff")
    assert_allowed("rm -rf build/")


def test_fail_open_on_garbage_input():
    result = subprocess.run(
        ["bash", str(GUARD)], input="not json", capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
