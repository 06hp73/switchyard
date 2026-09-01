"""The pre-push hook is the ROBUST enforcement layer for the protected
branch. Unlike git_guard.sh (which pattern-matches the shell command TEXT of
a Bash tool call before it runs, and is consequently beatable by enough
shell cleverness - see README.md's "Enforcement model" section), this hook
runs INSIDE git itself, after git has already fully resolved every ref and
remote involved in the push. There is no shell-string bypass left to find at
this layer - only a genuinely different repo/remote (this hook not being
installed there) or --no-verify (skipping client-side hooks entirely, a
limitation of every git hook, not just this one) can get past it.

Contract (see .git/hooks/pre-push.sample): git invokes the hook as
`<hook> <remote-name> <remote-url>` and feeds stdin lines of
`<local ref> <local oid> <remote ref> <remote oid>`. Exit 0 = allow the
push, nonzero = abort it (reason on stderr).
"""

import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "tools" / "guards" / "pre_push_hook.sh"
INSTALL = Path(__file__).resolve().parents[1] / "tools" / "guards" / "install_pre_push.sh"
LIB_DIR = Path(__file__).resolve().parents[1] / "tools" / "lib"

# Same rationale as test_git_guard.py: config_get.sh's sy_cfg_trusted shells
# out to a bare `python3`, which must be 3.11+ for tomllib.
_VENV_BIN = "/Users/storslasken/Developer/EV4XL-SIM/.venv/bin"

ZERO_SHA = "0" * 40
DEFAULT_URL = "https://github.com/06hp73/EV4SIM.git"


def _isolated_env(tmp_path: Path, switchyard_config: Path | None = None) -> dict:
    """An env with its own empty $HOME (so an ambient ~/.config/switchyard/
    config.toml on the machine running these tests can never leak in - same
    isolation test_git_guard.py's config tests use) and the venv's Python on
    PATH ahead of everything else (needed only when a test actually points
    SWITCHYARD_CONFIG somewhere, but harmless to include always)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": _VENV_BIN + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(home),
    }
    if switchyard_config is not None:
        env["SWITCHYARD_CONFIG"] = str(switchyard_config)
    else:
        env.pop("SWITCHYARD_CONFIG", None)
    return env


def run_hook(
    tmp_path: Path,
    stdin_lines: list[str],
    remote: str = "origin",
    url: str = DEFAULT_URL,
    switchyard_config: Path | None = None,
    hook_path: Path = HOOK,
) -> subprocess.CompletedProcess:
    """Drive the hook exactly as git would: exec it directly (so its own
    #!/bin/sh shebang and executable bit are exercised too, not bypassed by
    forcing an interpreter), argv = remote name + remote url, stdin = the
    ref-update lines."""
    stdin = "".join(line + "\n" for line in stdin_lines)
    return subprocess.run(
        [str(hook_path), remote, url],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env=_isolated_env(tmp_path, switchyard_config),
        check=False,
    )


def write_config(tmp_path: Path, body: str, name: str = "switchyard.toml") -> Path:
    cfg = tmp_path / name
    cfg.write_text(body)
    return cfg


# --- direct-drive: the hook script itself ------------------------------------


def test_blocks_push_to_protected_branch_on_product_remote(tmp_path):
    result = run_hook(tmp_path, ["refs/heads/x deadbeef refs/heads/main cafebabe"])
    assert result.returncode == 1, result.stderr
    assert "refs/heads/main" in result.stderr
    assert "train" in result.stderr


def test_allows_push_to_feature_branch(tmp_path):
    result = run_hook(tmp_path, ["refs/heads/x deadbeef refs/heads/feature cafebabe"])
    assert result.returncode == 0, result.stderr


def test_blocks_delete_of_protected_branch(tmp_path):
    # A delete sends the ZERO oid as the LOCAL side of the update (nothing
    # local maps to it) with the protected branch still named on the remote
    # side - "git push origin --delete main" / "git push origin :main" both
    # produce exactly this line shape.
    result = run_hook(tmp_path, [f"refs/heads/x {ZERO_SHA} refs/heads/main cafebabe"])
    assert result.returncode == 1, result.stderr
    assert "refs/heads/main" in result.stderr


def test_empty_stdin_allows(tmp_path):
    result = run_hook(tmp_path, [])
    assert result.returncode == 0, result.stderr


def test_default_product_remote_match_always_enforces_regardless_of_remote(tmp_path):
    # Deliberately different from git_guard.sh's own default: git_guard.sh
    # is ONE PreToolUse hook watching bash commands from EVERY repo a
    # session might sit in, so an unconfigured product_remote_match there
    # falls back to a hardcoded EV4SIM match (else it would falsely protect
    # unrelated repos, e.g. switchyard's own main). This hook is installed
    # ONE REPO AT A TIME, on purpose, by install_pre_push.sh - installing it
    # somewhere means protecting THAT repo, whatever its remote is named.
    result = run_hook(
        tmp_path,
        ["refs/heads/x deadbeef refs/heads/main cafebabe"],
        url="https://example.com/totally/unrelated/repo.git",
    )
    assert result.returncode == 1, result.stderr


def test_allows_non_product_remote_when_product_remote_match_configured(tmp_path):
    cfg = write_config(tmp_path, '[switchyard]\nproduct_remote_match = "06hp73/EV4SIM"\n')
    result = run_hook(
        tmp_path,
        ["refs/heads/x deadbeef refs/heads/main cafebabe"],
        url="https://github.com/someone-else/unrelated.git",
        switchyard_config=cfg,
    )
    assert result.returncode == 0, result.stderr


def test_blocks_product_remote_when_product_remote_match_configured(tmp_path):
    cfg = write_config(tmp_path, '[switchyard]\nproduct_remote_match = "06hp73/EV4SIM"\n')
    result = run_hook(
        tmp_path,
        ["refs/heads/x deadbeef refs/heads/main cafebabe"],
        url=DEFAULT_URL,
        switchyard_config=cfg,
    )
    assert result.returncode == 1, result.stderr


def test_configured_protected_branch_trunk(tmp_path):
    cfg = write_config(tmp_path, '[switchyard]\nprotected_branch = "trunk"\n')
    blocked = run_hook(
        tmp_path, ["refs/heads/x deadbeef refs/heads/trunk cafebabe"], switchyard_config=cfg
    )
    assert blocked.returncode == 1, blocked.stderr
    assert "refs/heads/trunk" in blocked.stderr

    allowed = run_hook(
        tmp_path, ["refs/heads/x deadbeef refs/heads/main cafebabe"], switchyard_config=cfg
    )
    assert allowed.returncode == 0, allowed.stderr


def test_blocks_when_any_line_among_several_targets_protected_branch(tmp_path):
    # A single "git push" can update multiple refs at once - one bad ref
    # among several must still abort the WHOLE push (git's own contract: any
    # nonzero exit rejects everything, not just the offending ref).
    result = run_hook(
        tmp_path,
        [
            "refs/heads/x deadbeef refs/heads/feature-a cafebabe",
            "refs/heads/y deadbeef refs/heads/main cafebabe",
            "refs/heads/z deadbeef refs/heads/feature-b cafebabe",
        ],
    )
    assert result.returncode == 1, result.stderr


# --- install_pre_push.sh -----------------------------------------------------


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def run_install(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        capture_output=True,
        text=True,
        timeout=15,
        env=_isolated_env(tmp_path),
        check=False,
    )


def test_install_fresh_symlink_default_mode(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run_install(["--repo", str(repo)], tmp_path)
    assert result.returncode == 0, result.stderr

    hook_path = repo / ".git" / "hooks" / "pre-push"
    assert hook_path.is_symlink()
    assert os.readlink(hook_path) == str(HOOK)


def test_install_idempotent_rerun_succeeds(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run_install(["--repo", str(repo)], tmp_path)
    assert first.returncode == 0, first.stderr
    second = run_install(["--repo", str(repo)], tmp_path)
    assert second.returncode == 0, second.stderr
    hook_path = repo / ".git" / "hooks" / "pre-push"
    assert os.readlink(hook_path) == str(HOOK)


def test_install_copy_mode_bakes_in_lib_dir_and_still_resolves_config(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run_install(["--repo", str(repo), "--mode", "copy"], tmp_path)
    assert result.returncode == 0, result.stderr

    hook_path = repo / ".git" / "hooks" / "pre-push"
    assert not hook_path.is_symlink()
    content = hook_path.read_text()
    assert f'SWITCHYARD_LIB_DIR_OVERRIDE="{LIB_DIR}"' in content

    # Prove it is not just a dead constant: a standalone copy, driven with a
    # config that changes the protected branch, must resolve it exactly like
    # the original - i.e. it genuinely found config_get.sh via the baked-in
    # override, not merely fallen back to its own hardcoded default.
    cfg = write_config(tmp_path, '[switchyard]\nprotected_branch = "trunk"\n')
    result = run_hook(
        tmp_path,
        ["refs/heads/x deadbeef refs/heads/trunk cafebabe"],
        switchyard_config=cfg,
        hook_path=hook_path,
    )
    assert result.returncode == 1, result.stderr


def test_install_symlink_mode_resolves_config_through_the_symlink(tmp_path):
    # The installed file at .git/hooks/pre-push is a SYMLINK whose $0, as
    # git execs it, is that symlink's own path in the target repo - proving
    # the hook's self-resolution finds ../lib/config_get.sh from THROUGH the
    # symlink (not just luckily matching hardcoded defaults) needs a
    # non-default config value to actually change the outcome.
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run_install(["--repo", str(repo)], tmp_path)
    assert result.returncode == 0, result.stderr
    hook_path = repo / ".git" / "hooks" / "pre-push"

    cfg = write_config(tmp_path, '[switchyard]\nprotected_branch = "trunk"\n')
    blocked = run_hook(
        tmp_path,
        ["refs/heads/x deadbeef refs/heads/trunk cafebabe"],
        switchyard_config=cfg,
        hook_path=hook_path,
    )
    assert blocked.returncode == 1, blocked.stderr
    allowed = run_hook(
        tmp_path,
        ["refs/heads/x deadbeef refs/heads/main cafebabe"],
        switchyard_config=cfg,
        hook_path=hook_path,
    )
    assert allowed.returncode == 0, allowed.stderr


def test_install_refuses_foreign_hook_without_force_or_chain(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    hook_path = repo / ".git" / "hooks" / "pre-push"
    original = "#!/bin/sh\nexit 0\n"
    hook_path.write_text(original)
    hook_path.chmod(0o755)

    result = run_install(["--repo", str(repo)], tmp_path)
    assert result.returncode != 0
    assert "--force" in result.stderr
    assert "--chain" in result.stderr
    # refused cleanly: nothing was touched
    assert hook_path.read_text() == original
    assert not hook_path.is_symlink()


def test_install_force_overwrites_and_backs_up_the_original(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    hook_path = repo / ".git" / "hooks" / "pre-push"
    hook_path.write_text("#!/bin/sh\necho ORIGINAL_HOOK_RAN >&2\nexit 0\n")
    hook_path.chmod(0o755)

    result = run_install(["--repo", str(repo), "--force"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert hook_path.is_symlink()
    assert os.readlink(hook_path) == str(HOOK)

    backup = repo / ".git" / "hooks" / "pre-push.pre-switchyard-backup"
    assert "ORIGINAL_HOOK_RAN" in backup.read_text()

    # the backed-up hook no longer runs at all now - only the switchyard
    # guard does, driven directly to confirm it behaves like any other
    # plain (non-chained) install.
    blocked = run_hook(tmp_path, ["refs/heads/x deadbeef refs/heads/main cafebabe"])
    assert blocked.returncode == 1, blocked.stderr


def test_install_chain_runs_switchyard_guard_first_then_falls_through(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    hook_path = repo / ".git" / "hooks" / "pre-push"
    hook_path.write_text("#!/bin/sh\necho ORIGINAL_HOOK_RAN >&2\nexit 0\n")
    hook_path.chmod(0o755)

    result = run_install(["--repo", str(repo), "--chain"], tmp_path)
    assert result.returncode == 0, result.stderr

    # protected branch: switchyard guard rejects BEFORE the original hook
    # ever runs - proven by its stderr marker being absent.
    blocked = subprocess.run(
        [str(hook_path), "origin", DEFAULT_URL],
        input="refs/heads/x deadbeef refs/heads/main cafebabe\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    assert "ORIGINAL_HOOK_RAN" not in blocked.stderr

    # feature branch: switchyard guard allows, falls through to the
    # original hook, whose own exit code and output both surface.
    allowed = subprocess.run(
        [str(hook_path), "origin", DEFAULT_URL],
        input="refs/heads/x deadbeef refs/heads/feature cafebabe\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "ORIGINAL_HOOK_RAN" in allowed.stderr


def test_install_chain_idempotent_does_not_double_backup_or_nest(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    hook_path = repo / ".git" / "hooks" / "pre-push"
    hook_path.write_text("#!/bin/sh\necho ORIGINAL_HOOK_RAN >&2\nexit 0\n")
    hook_path.chmod(0o755)

    first = run_install(["--repo", str(repo), "--chain"], tmp_path)
    assert first.returncode == 0, first.stderr
    backup = repo / ".git" / "hooks" / "pre-push.pre-switchyard-backup"
    backup_content_after_first = backup.read_text()

    second = run_install(["--repo", str(repo), "--chain"], tmp_path)
    assert second.returncode == 0, second.stderr
    assert backup.read_text() == backup_content_after_first

    allowed = subprocess.run(
        [str(hook_path), "origin", DEFAULT_URL],
        input="refs/heads/x deadbeef refs/heads/feature cafebabe\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "ORIGINAL_HOOK_RAN" in allowed.stderr


# --- the real thing: an actual bare origin and actual `git push` ------------


def _remote_ref_sha(repo_path: Path, ref: str) -> str:
    out = subprocess.run(
        ["git", "ls-remote", str(repo_path), ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()[0] if out.strip() else ""


def test_real_push_to_main_and_a_string_guard_bypass_are_both_rejected(tmp_path):
    # This is the whole point of this hook: prove it catches what
    # git_guard.sh's TEXT matching structurally cannot. "git push origin
    # \main" is byte-for-byte the same push as "git push origin main" once
    # the shell removes the backslash before git ever sees the argument -
    # exactly the class of trick the audit found beats string-matching. A
    # hook driven by git's own resolved refs never sees the "\" at all.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.name", "T"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"],
        check=True,
        capture_output=True,
    )

    install_result = run_install(["--repo", str(clone)], tmp_path)
    assert install_result.returncode == 0, install_result.stderr

    subprocess.run(
        ["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "second"],
        check=True,
        capture_output=True,
    )

    before = _remote_ref_sha(origin, "refs/heads/main")
    assert before, "seed push did not land"

    direct = subprocess.run(
        ["git", "-C", str(clone), "push", "origin", "HEAD:main"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert direct.returncode != 0, direct.stdout + direct.stderr
    assert _remote_ref_sha(origin, "refs/heads/main") == before

    bypass = subprocess.run(
        "git push origin \\main",
        shell=True,
        cwd=str(clone),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert bypass.returncode != 0, bypass.stdout + bypass.stderr
    assert _remote_ref_sha(origin, "refs/heads/main") == before

    feature = subprocess.run(
        ["git", "-C", str(clone), "push", "origin", "HEAD:feature"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert feature.returncode == 0, feature.stdout + feature.stderr
    assert _remote_ref_sha(origin, "refs/heads/feature") != ""
