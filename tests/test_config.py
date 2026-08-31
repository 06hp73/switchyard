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

from switchyard_config import SwitchyardConfig, dump_effective, load_config  # noqa: E402


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
