"""SwitchyardConfig: the project config layer (switchyard.toml).

Resolution order (first hit wins): $SWITCHYARD_CONFIG env var, then
<git toplevel of start-or-cwd>/switchyard.toml, then
~/.config/switchyard/config.toml, else all dataclass defaults.

A broken config (missing env-pointed file, malformed TOML, unknown key) must
never raise - the bash guards and the train depend on load_config() always
returning a usable config, warning on stderr instead of crashing.
"""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "lib"))

from switchyard_config import SwitchyardConfig, dump_effective, load_config


def test_defaults_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate from a real ~/.config/switchyard
    cfg = load_config(tmp_path)  # tmp_path has no .git and no switchyard.toml
    assert cfg == SwitchyardConfig()
    assert cfg.protected_branch == "main"
    assert cfg.wip_cap == 5
    assert cfg.gate_timeout == 5400
    assert cfg.batch == 1
    assert cfg.priority_label == "train-priority"
    assert cfg.live_prefixes == ("claude/", "fix/", "feat/")
    assert cfg.gate_fast == ""
    assert cfg.notify == "none"


def test_env_var_takes_priority_over_repo_config(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "switchyard.toml").write_text("[switchyard]\nwip_cap = 9\n")

    explicit = tmp_path / "explicit.toml"
    explicit.write_text("[switchyard]\nwip_cap = 3\n")

    monkeypatch.setenv("SWITCHYARD_CONFIG", str(explicit))
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = load_config(repo)
    assert cfg.wip_cap == 3  # env var wins even though repo config also sets wip_cap


def test_repo_toplevel_discovery_from_a_nested_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "trunk"\n')

    cfg = load_config(nested)  # started deep inside the repo, must still find the toplevel
    assert cfg.protected_branch == "trunk"


def test_no_repo_config_falls_back_to_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # a repo, but no switchyard.toml in it

    home_cfg_dir = tmp_path / ".config" / "switchyard"
    home_cfg_dir.mkdir(parents=True)
    (home_cfg_dir / "config.toml").write_text("[switchyard]\nwip_cap = 11\n")

    cfg = load_config(repo)
    assert cfg.wip_cap == 11


def test_malformed_toml_falls_back_to_defaults_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text("this is not [ valid toml at all =")

    cfg = load_config(repo)
    assert cfg == SwitchyardConfig()
    err = capsys.readouterr().err
    assert "switchyard" in err.lower()


def test_unknown_key_warns_but_known_keys_still_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text(
        '[switchyard]\nwip_cap = 7\nnot_a_real_key = "surprise"\n'
    )

    cfg = load_config(repo)
    assert cfg.wip_cap == 7
    err = capsys.readouterr().err
    assert "not_a_real_key" in err


# --- I8: config values are type-checked/coerced, never trusted verbatim ---
#
# dataclasses do not enforce their own type hints at construction time, so
# without coercion a TOML author's typo flows straight into a frozen
# SwitchyardConfig unchanged: gate_timeout = "5400" (a quoted string) later
# breaks subprocess.Popen.communicate(timeout=...), which requires a real
# number; live_prefixes = "claude/" (a bare string instead of an array)
# silently becomes a per-CHARACTER tuple via tuple("claude/") wherever a
# caller does tuple(cfg.live_prefixes). Each bad key must warn and fall back
# to THAT field's own default - never raise, and never take any other,
# validly-configured key down with it.


def test_gate_timeout_numeric_string_is_coerced_to_int(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\ngate_timeout = "5400"\n')

    cfg = load_config(repo)
    assert cfg.gate_timeout == 5400
    assert isinstance(cfg.gate_timeout, int)


def test_gate_timeout_non_numeric_string_warns_and_falls_back_to_default(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\ngate_timeout = "soon-ish"\n')

    cfg = load_config(repo)
    assert cfg.gate_timeout == 5400  # the field's own default, not a crash
    err = capsys.readouterr().err
    assert "gate_timeout" in err


def test_batch_numeric_string_is_coerced_to_int(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nbatch = "3"\n')

    cfg = load_config(repo)
    assert cfg.batch == 3
    assert isinstance(cfg.batch, int)


def test_wip_cap_bool_warns_and_falls_back_to_default(tmp_path, monkeypatch, capsys):
    # bool is a subclass of int in Python - true/false must not sneak
    # through an int field as 1/0.
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text("[switchyard]\nwip_cap = true\n")

    cfg = load_config(repo)
    assert cfg.wip_cap == 5
    err = capsys.readouterr().err
    assert "wip_cap" in err


def test_retry_flaky_non_bool_warns_and_falls_back_to_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nretry_flaky = "yes"\n')

    cfg = load_config(repo)
    assert cfg.retry_flaky is True  # the field's own default
    err = capsys.readouterr().err
    assert "retry_flaky" in err


def test_live_prefixes_scalar_string_ending_in_slash_wraps_as_one_prefix(
    tmp_path, monkeypatch, capsys
):
    # The exact scenario named in the audit: a config author writes
    # live_prefixes = "claude/" (forgetting the brackets) instead of
    # ["claude/"]. tuple("claude/") would silently produce a per-character
    # tuple; this must instead warn and wrap it as one prefix.
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nlive_prefixes = "claude/"\n')

    cfg = load_config(repo)
    assert cfg.live_prefixes == ("claude/",)
    err = capsys.readouterr().err
    assert "live_prefixes" in err


def test_live_prefixes_scalar_string_not_a_prefix_falls_back_to_default(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nlive_prefixes = "oops"\n')

    cfg = load_config(repo)
    assert cfg.live_prefixes == ("claude/", "fix/", "feat/")
    err = capsys.readouterr().err
    assert "live_prefixes" in err


def test_live_prefixes_list_with_non_string_element_falls_back_to_default(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text("[switchyard]\nlive_prefixes = [1, 2]\n")

    cfg = load_config(repo)
    assert cfg.live_prefixes == ("claude/", "fix/", "feat/")
    err = capsys.readouterr().err
    assert "live_prefixes" in err


def test_station_non_string_falls_back_to_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text("[switchyard]\nstation = 5\n")

    cfg = load_config(repo)
    assert cfg.station == ""
    err = capsys.readouterr().err
    assert "station" in err


def test_one_bad_typed_key_does_not_break_other_keys(tmp_path, monkeypatch, capsys):
    # A typo'd gate_timeout must not take a validly-configured wip_cap down
    # with it - each key is coerced (and, on failure, defaulted)
    # independently.
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\ngate_timeout = "nope"\nwip_cap = 9\n')

    cfg = load_config(repo)
    assert cfg.wip_cap == 9
    assert cfg.gate_timeout == 5400
    err = capsys.readouterr().err
    assert "gate_timeout" in err


def test_live_prefixes_array_parses_to_a_tuple(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nlive_prefixes = ["release/", "hotfix/"]\n')

    cfg = load_config(repo)
    assert cfg.live_prefixes == ("release/", "hotfix/")
    assert isinstance(cfg.live_prefixes, tuple)


def test_missing_env_pointed_file_warns_and_falls_back_to_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWITCHYARD_CONFIG", str(tmp_path / "does-not-exist.toml"))
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = load_config(tmp_path)
    assert cfg == SwitchyardConfig()
    assert capsys.readouterr().err  # warned, did not silently continue


def test_default_retry_flaky_is_true(tmp_path, monkeypatch):
    # True is the owner-approved default for real (CLI/config-driven) runs -
    # merge_train.run_train()/process_branch() themselves default this
    # parameter to False so every caller that predates the feature (every
    # test that never mentions retry_flaky) keeps running the gate once.
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(tmp_path)
    assert cfg.retry_flaky is True


def test_retry_flaky_can_be_disabled_via_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text("[switchyard]\nretry_flaky = false\n")

    cfg = load_config(repo)
    assert cfg.retry_flaky is False


# --- trusted_only: guard-scoping keys must ignore repo-local config --------
#
# git_guard.sh's main-push ban reads protected_branch/product_remote_match to
# decide what it is protecting. A repo-local switchyard.toml can ship in the
# very same PR/branch the guard is supposed to be judging, so those two keys
# must be resolvable in a mode that never even looks at a repo-local file -
# only $SWITCHYARD_CONFIG or ~/.config/switchyard/config.toml, both outside a
# PR author's control.


def test_trusted_only_ignores_repo_local_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "attacker"\n')

    cfg = load_config(repo, trusted_only=True)
    assert cfg.protected_branch == "main"  # repo-local config is invisible in trusted-only mode


def test_trusted_only_still_honors_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "attacker"\n')

    home_cfg_dir = tmp_path / ".config" / "switchyard"
    home_cfg_dir.mkdir(parents=True)
    (home_cfg_dir / "config.toml").write_text('[switchyard]\nprotected_branch = "trunk"\n')

    cfg = load_config(repo, trusted_only=True)
    assert cfg.protected_branch == "trunk"  # user-level config still applies


def test_trusted_only_still_honors_env_var(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "attacker"\n')

    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[switchyard]\nprotected_branch = "env-wins"\n')
    monkeypatch.setenv("SWITCHYARD_CONFIG", str(explicit))
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = load_config(repo, trusted_only=True)
    assert cfg.protected_branch == "env-wins"


def test_non_trusted_load_still_honors_repo_local_by_default(tmp_path, monkeypatch):
    # trusted_only defaults to False - existing (non-guard) callers such as
    # the train/radar/status keep today's repo-local-config behavior, since
    # a project's own trunk name is exactly the kind of thing repo-local
    # config SHOULD be able to set for itself.
    monkeypatch.delenv("SWITCHYARD_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "trunk"\n')

    cfg = load_config(repo)
    assert cfg.protected_branch == "trunk"


def test_config_is_frozen():
    cfg = SwitchyardConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.wip_cap = 99


def test_dump_effective_mentions_every_field():
    cfg = SwitchyardConfig(wip_cap=42)
    dumped = dump_effective(cfg)
    for field in dataclasses.fields(SwitchyardConfig):
        assert field.name in dumped
    assert "42" in dumped


# --- switchyard_config_cli.py: the bash-facing entry point ------------------

CLI_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "lib" / "switchyard_config_cli.py"


def run_cli(key: str, default: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), key, default],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )


def test_cli_prints_configured_value_when_config_present(tmp_path):
    config = tmp_path / "cfg.toml"
    config.write_text("[switchyard]\nwip_cap = 2\n")
    proc = run_cli(
        "wip_cap", "5", env={**os.environ, "SWITCHYARD_CONFIG": str(config), "HOME": str(tmp_path)}
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2"


def test_cli_prints_default_for_unknown_key(tmp_path):
    config = tmp_path / "cfg.toml"
    config.write_text("[switchyard]\nwip_cap = 2\n")
    proc = run_cli(
        "not_a_field",
        "fallback-value",
        env={**os.environ, "SWITCHYARD_CONFIG": str(config), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "fallback-value"


def test_cli_joins_tuple_fields_with_commas(tmp_path):
    config = tmp_path / "cfg.toml"
    config.write_text('[switchyard]\nlive_prefixes = ["release/", "hotfix/"]\n')
    proc = run_cli(
        "live_prefixes",
        "claude/,fix/,feat/",
        env={**os.environ, "SWITCHYARD_CONFIG": str(config), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "release/,hotfix/"


def test_cli_falls_back_to_its_own_default_when_no_config_present(tmp_path):
    # No SWITCHYARD_CONFIG, HOME redirected away from any real ~/.config -
    # load_config() itself returns pure defaults, and wip_cap's default (5)
    # happens to equal the CLI default passed here, so this also covers the
    # plain "nothing configured at all" path end to end through the CLI.
    proc = run_cli("wip_cap", "5", env={**os.environ, "HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "5"


def test_cli_trusted_only_flag_ignores_repo_local_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "attacker"\n')

    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--trusted-only", "protected_branch", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "main"  # repo-local config ignored, falls to the CLI's default


def test_cli_without_trusted_only_flag_honors_repo_local_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "switchyard.toml").write_text('[switchyard]\nprotected_branch = "attacker"\n')

    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "protected_branch", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "attacker"  # non-trusted path still reads repo-local config
