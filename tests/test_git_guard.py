"""The guard script blocks git operations that are unsafe with parallel sessions.

Contract: stdin = PreToolUse JSON; exit 0 = allow, exit 2 = block (reason on
stderr). The script must never block on parse errors (fail-open, exit 0):
a broken guard must not paralyze every session.
"""

import json
import os
import subprocess
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "tools" / "guards" / "git_guard.sh"

# The EV4XL-SIM venv is the only Python 3.11+ interpreter available in this
# environment (tomllib requires it); config_get.sh's `sy_cfg` shells out to a
# bare `python3`, so PATH must put a working interpreter ahead of whatever
# this process's own ambient PATH would otherwise resolve first.
_VENV_BIN = "/Users/storslasken/Developer/EV4XL-SIM/.venv/bin"


def run_guard(command: str, cwd: str = "/tmp") -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd})
    return subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
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


def test_repo_local_config_cannot_disarm_main_push_ban(tmp_path):
    # A switchyard.toml sitting in the repo itself (e.g. shipped by a PR)
    # must NEVER be able to change product_remote_match/protected_branch -
    # those two are guard-scoping and trusted-only (sy_cfg_trusted). No
    # SWITCHYARD_CONFIG and an isolated HOME here: the repo-local file is the
    # only config source in play, and it must be completely invisible.
    ev4sim_repo = make_repo_with_origin(
        tmp_path, "ev4sim-repo-local", "https://github.com/06hp73/EV4SIM.git"
    )
    (ev4sim_repo / "switchyard.toml").write_text(
        '[switchyard]\nproduct_remote_match = "nonsense-does-not-match-anything"\n'
    )
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(isolated_home),
    }
    payload = json.dumps(
        {"tool_input": {"command": "git push origin main"}, "cwd": str(ev4sim_repo)}
    )
    # cwd=ev4sim_repo is load-bearing: _sy_config_present's repo-toplevel
    # discovery (`git rev-parse --show-toplevel`) resolves against the
    # GUARD PROCESS's own OS-level cwd, not the hook JSON's "cwd" field
    # (which only feeds the separate `git -C "$CWD"` origin lookup) - a real
    # PreToolUse hook always runs with its process cwd inside the repo being
    # worked on, so this matches actual usage.
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=str(ev4sim_repo),
        check=False,
    )
    assert result.returncode == 2, (
        f"repo-local switchyard.toml must not disarm the main-push ban: {result.stdout}"
    )
    assert "train" in result.stderr


def test_repo_local_config_cannot_override_guard_scoping_even_with_home_config_present(tmp_path):
    # Stronger version of the test above: force sy_cfg_trusted to actually
    # shell out to the python CLI (by making a trusted home config exist),
    # and prove a co-existing repo-local switchyard.toml still contributes
    # NOTHING to either guard-scoping key - not protected_branch, not
    # product_remote_match - even though the python path is genuinely
    # exercised this time, not just short-circuited by the bash presence
    # check.
    ev4sim_repo = make_repo_with_origin(
        tmp_path, "ev4sim-both-configs", "https://github.com/06hp73/EV4SIM.git"
    )
    (ev4sim_repo / "switchyard.toml").write_text(
        '[switchyard]\nprotected_branch = "not-main"\n'
        'product_remote_match = "nonsense-does-not-match-anything"\n'
    )
    fake_home = tmp_path / "fake-home-2"
    (fake_home / ".config" / "switchyard").mkdir(parents=True)
    # Present but empty - just enough for _sy_config_present_trusted to see a
    # real file and invoke the python CLI, without itself setting anything.
    (fake_home / ".config" / "switchyard" / "config.toml").write_text("[switchyard]\n")

    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(fake_home),
    }
    payload = json.dumps(
        {"tool_input": {"command": "git push origin main"}, "cwd": str(ev4sim_repo)}
    )
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=str(ev4sim_repo),  # see comment in the test above - load-bearing
        check=False,
    )
    assert result.returncode == 2, (
        f"repo-local switchyard.toml must stay invisible to guard-scoping keys "
        f"even when a trusted home config is also present: {result.stdout}"
    )
    assert "train" in result.stderr


def test_home_config_can_still_change_product_remote_match(tmp_path):
    # Unlike a repo-local file, ~/.config/switchyard/config.toml IS trusted
    # (outside a PR author's control) and may still retarget which repo is
    # protected.
    fake_home = tmp_path / "fake-home"
    (fake_home / ".config" / "switchyard").mkdir(parents=True)
    (fake_home / ".config" / "switchyard" / "config.toml").write_text(
        '[switchyard]\nproduct_remote_match = "06hp73/OTHER"\n'
    )
    ev4sim_repo = make_repo_with_origin(
        tmp_path, "ev4sim-home-cfg", "https://github.com/06hp73/EV4SIM.git"
    )
    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(fake_home),
    }
    payload = json.dumps(
        {"tool_input": {"command": "git push origin main"}, "cwd": str(ev4sim_repo)}
    )
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=str(ev4sim_repo),
        check=False,
    )
    assert result.returncode == 0, (
        f"~/.config/switchyard/config.toml should be trusted enough to retarget "
        f"product_remote_match: {result.stderr}"
    )


def test_home_config_can_still_change_protected_branch(tmp_path):
    fake_home = tmp_path / "fake-home-3"
    (fake_home / ".config" / "switchyard").mkdir(parents=True)
    (fake_home / ".config" / "switchyard" / "config.toml").write_text(
        '[switchyard]\nprotected_branch = "trunk"\n'
    )
    ev4sim_repo = make_repo_with_origin(
        tmp_path, "ev4sim-trunk", "https://github.com/06hp73/EV4SIM.git"
    )
    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(fake_home),
    }
    # "main" is no longer protected once "trunk" is configured as the trunk...
    allowed = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps(
            {"tool_input": {"command": "git push origin main"}, "cwd": str(ev4sim_repo)}
        ),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=str(ev4sim_repo),
        check=False,
    )
    assert allowed.returncode == 0, f"main should no longer be protected: {allowed.stderr}"

    # ...and "trunk" is blocked instead.
    blocked = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps(
            {"tool_input": {"command": "git push origin trunk"}, "cwd": str(ev4sim_repo)}
        ),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=str(ev4sim_repo),
        check=False,
    )
    assert blocked.returncode == 2, "trunk is now the configured protected branch"
    assert "train" in blocked.stderr


def test_allows_feature_branch_push():
    # NOTE: a bare "git push" used to be asserted allowed unconditionally
    # right here - that was the C3 vulnerability (see
    # test_bare_push_depends_on_current_branch below): a bare push's real
    # destination is whatever the current branch's upstream is, which this
    # test's default cwd="/tmp" can never determine, so the only fail-safe
    # answer for THAT cwd is "blocked". Bare push now has its own dedicated,
    # branch-aware tests instead of a single unconditional assertion here.
    assert_allowed("git push -u origin claude/parallel-fix-collisions-b2d1f3")
    assert_allowed("rtk git push origin fix/fcr-export-adequacy")
    assert_allowed("git push origin main-feature")
    assert_allowed("git push origin feature-main")
    assert_allowed("git push origin feature/main-fix")
    assert_allowed("git push origin claude/a:claude/a")
    assert_allowed("git push origin HEAD:claude/refresh")


def make_repo_on_branch(base: Path, name: str, branch: str) -> Path:
    """A real tmp git repo checked out on `branch`, no commits needed -
    `git symbolic-ref --short HEAD` resolves correctly from an unborn branch
    (created via `git init -b`) since it just reads what HEAD points to."""
    repo = base / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, capture_output=True)
    return repo


def test_bare_push_depends_on_current_branch(tmp_path):
    # A bare "git push" (or "git push origin", "git push -u origin") carries
    # no explicit refspec at all: its real destination is whatever the
    # current branch's upstream is, which the command STRING alone can never
    # show - it depends on which branch this runs from. The guard must
    # resolve that from the hook's own cwd instead of assuming either way.
    on_main = make_repo_on_branch(tmp_path, "bare-on-main", "main")
    assert_blocked("git push", "train", cwd=str(on_main))
    assert_blocked("git push origin", "train", cwd=str(on_main))
    assert_blocked("git push -u origin", "train", cwd=str(on_main))

    on_feature = make_repo_on_branch(tmp_path, "bare-on-feature", "claude/x")
    assert_allowed("git push", cwd=str(on_feature))
    assert_allowed("git push origin", cwd=str(on_feature))
    assert_allowed("git push -u origin", cwd=str(on_feature))
    assert_allowed("git push origin claude/x", cwd=str(on_feature))


def test_push_head_depends_on_current_branch(tmp_path):
    # "git push origin HEAD" with no colon has an implicit destination too -
    # HEAD pushes the current branch to the same-named branch on the remote
    # by default - so it is exactly as branch-dependent as a bare push.
    on_main = make_repo_on_branch(tmp_path, "head-on-main", "main")
    assert_blocked("git push origin HEAD", "train", cwd=str(on_main))

    on_feature = make_repo_on_branch(tmp_path, "head-on-feature", "claude/y")
    assert_allowed("git push origin HEAD", cwd=str(on_feature))


def test_blocks_quoted_main_refspec():
    # Wrapping the ref token in quotes used to be enough to slip past the
    # old boundary-anchored regex (the token became `"main"`, not `main`,
    # which never matched the whitespace-delimited pattern).
    assert_blocked('git push origin "main"', "train")
    assert_blocked("git push origin 'main'", "train")


def test_blocks_interior_quoted_main_refspec():
    # A real shell removes quote characters WHEREVER they sit in a word, not
    # just when they wrap the whole token: ma"in" and m"a"i"n" both become
    # the bare word "main" once actually run. The old stripping only
    # handled a token that starts AND ends with a matching quote
    # (\"*\"/'*') - neither of these tokens does, since the quotes sit
    # inside the word, so the old code compared the literal 8/9-character
    # quoted string against "main", never matched, and let the push through.
    assert_blocked('git push origin ma"in"', "train")
    assert_blocked('git push origin m"a"i"n"', "train")


def test_blocks_bare_at_sign_as_head_alias_on_protected_branch(tmp_path):
    # "@" is git's own shorthand for HEAD - "git push origin @" resolves
    # exactly like "git push origin HEAD", which the guard already handled;
    # "@" itself was never recognized as the same alias.
    on_main = make_repo_on_branch(tmp_path, "at-on-main", "main")
    assert_blocked("git push origin @", "train", cwd=str(on_main))

    on_feature = make_repo_on_branch(tmp_path, "at-on-feature", "claude/z")
    assert_allowed("git push origin @", cwd=str(on_feature))


def test_blocks_dash_capital_c_push_to_main():
    # "git -C <path> push ..." runs the push against a DIFFERENT repo, but
    # the old extraction keyed on the literal substring "git push" being
    # adjacent - "git -C /x push" was never even recognized as a push
    # invocation at all and sailed through unclassified.
    assert_blocked("git -C /some/other/repo push origin main", "train")


def test_blocks_dash_c_config_push_to_main():
    # Deliberately non-identity keys (foo=bar / core.pager=cat): a
    # user.name=/user.email= value would ALSO trip the separate, unrelated
    # identity-change rule, which would block for the wrong reason and mask
    # whether this C3 fix itself is doing anything.
    assert_blocked("git -c foo=bar push origin main", "train")
    assert_blocked("git -c core.pager=cat push origin main", "train")


def test_blocks_git_dir_and_work_tree_push_to_main():
    # Paths deliberately avoid containing the substring "git" anywhere (no
    # ".git", no "-git-"): the OLD extraction regex had no word-boundary
    # before its own literal "git", so a value like "/x/.git" would
    # accidentally, coincidentally self-match right there and get blocked
    # for the wrong reason, masking whether recognizing --git-dir/
    # --work-tree as flags is actually what is doing the blocking.
    assert_blocked("git --git-dir=/x/other-repo push origin main", "train")
    assert_blocked("git --git-dir /x/other-repo push origin main", "train")
    assert_blocked("git --work-tree=/x/other-repo push origin main", "train")
    assert_blocked("git --work-tree /x/other-repo push origin main", "train")


def test_blocks_multiple_global_options_before_push():
    assert_blocked("git -C /x -c foo=bar push origin main", "train")


def test_dash_capital_c_push_to_feature_branch_still_allowed():
    # -C must not become an automatic block for every push through it - an
    # explicit, non-protected-naming refspec is still safe and stays
    # allowed, exactly like it would with no -C at all.
    assert_allowed("git -C /x push origin claude/x")


def test_dash_c_push_to_feature_branch_still_allowed():
    assert_allowed("git -c foo=bar push origin claude/x")


def test_dash_capital_c_refspec_less_push_fails_safe(tmp_path):
    # -C points at a possibly different repo; THIS process's cwd/branch says
    # nothing about what is checked out there. A push with no explicit
    # destination under -C must refuse rather than guess - even run from a
    # cwd whose own current branch is an entirely harmless feature branch,
    # since trusting that branch would be answering the wrong question.
    on_feature = make_repo_on_branch(tmp_path, "dashC-refspecless", "claude/w")
    assert_blocked("git -C /some/other/repo push origin", "train", cwd=str(on_feature))
    assert_blocked("git -C /some/other/repo push", "train", cwd=str(on_feature))


def test_bare_push_fail_safe_when_branch_undeterminable():
    # cwd="/tmp" is not a git repo (or at least not one whose branch this
    # test controls) - current branch can't be resolved, so a bare push
    # must fail SAFE (blocked), not open.
    assert_blocked("git push", "train", cwd="/tmp")


def test_blocks_rm_rf_on_worktrees():
    assert_blocked("rm -rf .claude/worktrees/foo", "worktree remove")
    assert_blocked("rm -rf /Users/x/repo/.claude/worktrees", "worktree remove")


def test_blocks_rm_rf_on_configured_worktree_dir(tmp_path):
    # worktree_dir (used by `switchyard track new`/`done`) can point track
    # worktrees somewhere other than .claude/worktrees - the rm-rf ban must
    # follow it there, on top of (not instead of) the .claude/worktrees
    # default, which stays protected regardless of what worktree_dir names.
    config = tmp_path / "switchyard.toml"
    config.write_text('[switchyard]\nworktree_dir = "/opt/tracks"\n')
    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "SWITCHYARD_CONFIG": str(config),
    }

    configured = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps({"tool_input": {"command": "rm -rf /opt/tracks/foo"}, "cwd": "/tmp"}),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert configured.returncode == 2, configured.stdout
    assert "worktree remove" in configured.stderr

    default_still_active = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps(
            {"tool_input": {"command": "rm -rf .claude/worktrees/bar"}, "cwd": "/tmp"}
        ),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert default_still_active.returncode == 2, default_still_active.stdout

    unrelated_path_still_allowed = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps({"tool_input": {"command": "rm -rf build/"}, "cwd": "/tmp"}),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert unrelated_path_still_allowed.returncode == 0, unrelated_path_still_allowed.stderr


def test_allows_ordinary_commands():
    assert_allowed("git status")
    assert_allowed("git commit -m 'feat: x'")
    assert_allowed("ls -la && git diff")
    assert_allowed("rm -rf build/")


def test_fail_open_on_garbage_input():
    result = subprocess.run(
        ["bash", str(GUARD)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0


def test_main_push_ban_uses_configured_product_remote_match(tmp_path):
    # product_remote_match set in switchyard.toml replaces which origin
    # substring means "the protected product repo": EV4SIM (blocked by the
    # hardcoded default in every test above) becomes ALLOWED, and the newly
    # named repo becomes the one that's blocked instead - proving the config
    # value is actually read, not just tolerated.
    config = tmp_path / "switchyard.toml"
    config.write_text('[switchyard]\nproduct_remote_match = "06hp73/OTHER"\n')
    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "SWITCHYARD_CONFIG": str(config),
    }

    ev4sim_repo = make_repo_with_origin(tmp_path, "ev4sim", "https://github.com/06hp73/EV4SIM.git")
    payload = json.dumps(
        {"tool_input": {"command": "git push origin main"}, "cwd": str(ev4sim_repo)}
    )
    allowed = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert allowed.returncode == 0, (
        f"EV4SIM should be allowed once the config points 'protected' elsewhere: {allowed.stderr}"
    )

    other_repo = make_repo_with_origin(tmp_path, "other", "https://github.com/06hp73/OTHER.git")
    payload = json.dumps(
        {"tool_input": {"command": "git push origin main"}, "cwd": str(other_repo)}
    )
    blocked = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert blocked.returncode == 2, "OTHER is now the configured protected repo"
    assert "train" in blocked.stderr
