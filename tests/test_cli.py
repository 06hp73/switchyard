"""The unified `switchyard` CLI: status, stats, radar, land.

status/stats are new composed views; radar/land are thin passthroughs to the
existing collision_radar.py / merge_train.py modules - these tests check the
composition and the passthrough wiring, not radar/train behavior itself
(that is already covered by test_collision_radar.py / test_merge_train.py).

Uses the same real local git topology (bare 'origin' + a clone) as
test_merge_train.py's make_world().
"""

import json
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


def make_world(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(origin), str(seed)], check=True, capture_output=True, env=ENV
    )
    (seed / "app.txt").write_text("v1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "origin", "main")
    for name, content in [("claude/good", "good change\n"), ("claude/bad", "bad change\n")]:
        git(seed, "checkout", "-b", name, "main")
        (seed / f"{name.split('/')[1]}.txt").write_text(content)
        git(seed, "add", "-A")
        git(seed, "commit", "-m", name)
        git(seed, "push", "origin", name)
        git(seed, "checkout", "main")
    station = tmp_path / "station"
    subprocess.run(
        ["git", "clone", str(origin), str(station)], check=True, capture_output=True, env=ENV
    )
    # collision_radar/wip_status scan LOCAL branches (`refs/heads`), unlike
    # merge_train which only ever needs the remote-tracking `origin/<branch>`
    # refs a plain clone already has - so give station local branches too,
    # matching a real developer checkout with active work-track branches.
    for name in ("claude/good", "claude/bad"):
        git(station, "branch", name, f"origin/{name}")
    return origin, station


def origin_main(origin: Path) -> str:
    return git(origin, "rev-parse", "main")


def run_cli(
    *args: str, home: Path, timeout: int = 30, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**ENV, "HOME": str(home)},
        timeout=timeout,
        cwd=cwd,
        check=False,
    )


# --- switchyard status --------------------------------------------------------


def test_status_renders_all_sections_without_gh(tmp_path):
    origin, station = make_world(tmp_path)

    proc = run_cli("status", "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "WIP" in out
    assert "RADAR" in out
    assert "QUEUE" in out
    assert "FLAKY" in out
    assert "LAST LANDINGS" in out
    # gh is not on PATH (see ENV above) - the queue section must degrade
    # gracefully, never crash the whole status view.
    assert "gh unavailable" in out
    # WIP/RADAR need no gh at all and should show the two live branches.
    assert "claude/good" in out
    assert "claude/bad" in out
    # Neither .train/flaky_log.jsonl nor .train/history.jsonl exist yet in a
    # freshly cloned station that never trained - both sections must say so
    # instead of crashing on a missing file.
    assert "not present" in out or "no " in out.lower()


def test_status_survives_missing_train_dir_and_bad_repo_path(tmp_path):
    # Not even a .git at all: every section must degrade, never traceback.
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    proc = run_cli("status", "--repo", str(empty), home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr


def test_status_defaults_to_notify_none(tmp_path):
    origin, station = make_world(tmp_path)

    proc = run_cli("status", "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "notify: none" in proc.stdout


def test_status_flaky_section_shows_branch_and_tail_not_a_json_blob(tmp_path):
    # B4: _append_flaky_log now records which branch a retry-rescued gate
    # actually judged (see test_merge_train.py's flaky-log tests) - this
    # checks the OTHER half, that `switchyard status`'s FLAKY section
    # renders it as "branch - test tail (age)" instead of falling all the
    # way back to dumping the raw JSON entry (its old behavior, since
    # "branch" never used to be there for it to find).
    _origin, station = make_world(tmp_path)
    train_dir = station / ".train"
    train_dir.mkdir(exist_ok=True)
    entry = {
        "branch": "claude/flaky-one",
        "tree": "abc123",
        "gate": "bash tools/train/gate.sh",
        "first_tail": "AssertionError: boom in test_thing.py",
        "ts": time.time(),
    }
    (train_dir / "flaky_log.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    proc = run_cli("status", "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "claude/flaky-one - AssertionError: boom in test_thing.py" in proc.stdout
    assert '"branch"' not in proc.stdout  # never the raw JSON dump when branch IS present


def test_status_flaky_section_degrades_gracefully_for_pre_fix_entries(tmp_path):
    # A flaky_log.jsonl line written before B4 has no "branch" key at all -
    # the exact shape that used to render as a raw JSON blob. Must still
    # degrade to SOME readable label, never crash the whole status view.
    _origin, station = make_world(tmp_path)
    train_dir = station / ".train"
    train_dir.mkdir(exist_ok=True)
    entry = {"tree": "abc123", "gate": "x", "first_tail": "boom", "ts": time.time()}
    (train_dir / "flaky_log.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    proc = run_cli("status", "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr


def test_status_shows_configured_notify_mode(tmp_path):
    origin, station = make_world(tmp_path)
    (station / "switchyard.toml").write_text('[switchyard]\nnotify = "macos"\n')

    proc = run_cli("status", "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "notify: macos" in proc.stdout


def test_status_always_prints_resolved_repo_and_train_dir_presence(tmp_path):
    # B3: an empty status view (no .train/ yet) must never be mistaken for
    # "all quiet" - the resolved repo path and whether .train/ exists there
    # are always the first two lines, regardless of what else is going on.
    _origin, station = make_world(tmp_path)

    proc = run_cli("status", "--repo", str(station), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert f"repo: {station.resolve()}" in proc.stdout
    assert ".train/: NOT present" in proc.stdout  # make_world's station never ran a train

    (station / ".train").mkdir()
    proc = run_cli("status", "--repo", str(station), home=tmp_path)
    assert ".train/: present" in proc.stdout


# --- switchyard status/stats: --repo defaults to cfg.station, not cwd (B3) ----
#
# Train state (.train/history.jsonl, .train/flaky_log.jsonl) lives in
# whichever repo actually ran the train - the station clone
# (switchyard.toml's `station` field) - which is rarely the directory a
# human happens to be sitting in when they type `switchyard status`.
# Without an explicit --repo, that must be preferred over bare cwd.


def test_status_without_explicit_repo_defaults_to_configured_station(tmp_path):
    _origin, station = make_world(tmp_path)

    home = tmp_path / "home"
    config_dir = home / ".config" / "switchyard"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("config.toml").write_text(f'[switchyard]\nstation = "{station}"\n')

    # A neutral, non-git cwd: proves resolution comes from the configured
    # station, never from wherever the command happened to be invoked.
    neutral_cwd = tmp_path / "somewhere-else"
    neutral_cwd.mkdir()

    proc = run_cli("status", home=home, cwd=neutral_cwd)

    assert proc.returncode == 0, proc.stderr
    assert f"repo: {station.resolve()}" in proc.stdout
    assert "claude/good" in proc.stdout  # WIP section - proves it looked at the STATION


def test_stats_without_explicit_repo_defaults_to_configured_station(tmp_path):
    station = tmp_path / "station"
    station.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(station)],
        check=True,
        capture_output=True,
        env=ENV,
    )
    train_dir = station / ".train"
    train_dir.mkdir()
    entry = {
        "branch": "claude/x",
        "status": "landed",
        "detail_first_line": "",
        "gate_seconds": 1.0,
        "tree": "t1",
        "batch": 1,
        "ts": time.time(),
    }
    (train_dir / "history.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    home = tmp_path / "home"
    config_dir = home / ".config" / "switchyard"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("config.toml").write_text(f'[switchyard]\nstation = "{station}"\n')

    neutral_cwd = tmp_path / "somewhere-else"
    neutral_cwd.mkdir()

    proc = run_cli("stats", "--days", "1", home=home, cwd=neutral_cwd)

    assert proc.returncode == 0, proc.stderr
    assert f"repo: {station.resolve()}" in proc.stdout
    assert "1 landing attempt" in proc.stdout


def test_status_explicit_repo_still_wins_over_configured_station(tmp_path):
    _origin, station = make_world(tmp_path)

    other = tmp_path / "not-the-station"
    other.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(other)], check=True, capture_output=True, env=ENV
    )

    home = tmp_path / "home"
    config_dir = home / ".config" / "switchyard"
    config_dir.mkdir(parents=True)
    # station points at the real station, but an explicit --repo must win.
    config_dir.joinpath("config.toml").write_text(f'[switchyard]\nstation = "{station}"\n')

    proc = run_cli("status", "--repo", str(other), home=home)

    assert proc.returncode == 0, proc.stderr
    assert f"repo: {other.resolve()}" in proc.stdout
    assert "claude/good" not in proc.stdout  # did NOT silently look at the station instead


# --- switchyard stats ---------------------------------------------------------


def test_stats_aggregates_synthetic_history(tmp_path):
    repo = tmp_path / "repo"
    train_dir = repo / ".train"
    train_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=ENV
    )

    now = time.time()
    in_window = [
        {
            "branch": "claude/a",
            "status": "landed",
            "detail_first_line": "",
            "gate_seconds": 10.0,
            "tree": "t1",
            "batch": 1,
            "ts": now - 3600,
        },
        {
            "branch": "claude/b",
            "status": "landed",
            "detail_first_line": "",
            "gate_seconds": 20.0,
            "tree": "t2",
            "batch": 1,
            "ts": now - 7200,
        },
        {
            "branch": "claude/c",
            "status": "rejected",
            "detail_first_line": "gate failed",
            "gate_seconds": 4.0,
            "tree": "t3",
            "batch": 1,
            "ts": now - 100,
        },
        {
            "branch": "claude/c",
            "status": "rejected",
            "detail_first_line": "gate failed",
            "gate_seconds": 6.0,
            "tree": "t4",
            "batch": 1,
            "ts": now - 50,
        },
    ]
    out_of_window = [
        {
            "branch": "claude/old",
            "status": "landed",
            "detail_first_line": "",
            "gate_seconds": 999.0,
            "tree": "t5",
            "batch": 1,
            "ts": now - 30 * 86400,
        },
    ]
    history = train_dir / "history.jsonl"
    history.write_text(
        "\n".join(json.dumps(e) for e in (in_window + out_of_window)) + "\n", encoding="utf-8"
    )

    proc = run_cli("stats", "--repo", str(repo), "--days", "14", home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "4 landing attempt" in out  # only the 4 in-window entries counted
    assert "landed: 2" in out
    assert "rejected: 2" in out
    # mean=(10+20+4+6)/4=10.0, p90 (linear interpolation on [4,6,10,20])=17.0
    assert "mean 10.0s" in out
    assert "p90 17.0s" in out
    assert "claude/c: 2" in out  # top rejected branch, both its rejections counted


def test_stats_handles_missing_history_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=ENV
    )

    proc = run_cli("stats", "--repo", str(repo), home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr


# --- switchyard stats --days validation (B2: --days 0 used to ZeroDivisionError) --


def test_stats_days_zero_rejected_with_a_clean_error_not_a_traceback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    proc = run_cli("stats", "--repo", str(repo), "--days", "0", home=tmp_path)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert "ZeroDivisionError" not in proc.stderr
    assert "--days" in proc.stderr


def test_stats_days_negative_rejected_with_a_clean_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    proc = run_cli("stats", "--repo", str(repo), "--days", "-5", home=tmp_path)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr


def test_cmd_stats_clamps_non_positive_days_when_called_directly(tmp_path, capsys):
    # Defense in depth for any caller that builds its own Namespace and
    # calls cmd_stats directly, bypassing argparse's own --days validation
    # (_positive_days) entirely - must still never hit the ZeroDivisionError
    # `landed / days` used to raise for days <= 0 (cli.py's cmd_stats).
    import argparse

    sys.path.insert(0, str(CLI_SCRIPT.parent))
    import cli

    repo = tmp_path / "repo"
    repo.mkdir()

    rc = cli.cmd_stats(argparse.Namespace(repo=repo, days=0))

    assert rc == 0
    assert "last 1 day(s)" in capsys.readouterr().out


# --- _read_jsonl / stats: unbounded logs are tail-capped, not slurped whole (B5) --


def test_read_jsonl_caps_to_the_most_recent_max_lines(tmp_path):
    # A JSONL log here is strictly append-only, so the tail is always the
    # newest entries - a small max_lines keeps this test fast instead of
    # writing JSONL_TAIL_CAP (5000) real lines just to exercise the cap.
    sys.path.insert(0, str(CLI_SCRIPT.parent))
    import cli

    path = tmp_path / "log.jsonl"
    lines = [json.dumps({"n": i}) for i in range(10)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    entries = cli._read_jsonl(path, max_lines=3)

    assert [e["n"] for e in entries] == [7, 8, 9]


def test_stats_notes_the_cap_when_history_hits_it(tmp_path, capsys):
    sys.path.insert(0, str(CLI_SCRIPT.parent))
    import argparse

    import cli

    repo = tmp_path / "repo"
    train_dir = repo / ".train"
    train_dir.mkdir(parents=True)
    now = time.time()
    entries = [
        {"branch": f"claude/{i}", "status": "landed", "ts": now} for i in range(cli.JSONL_TAIL_CAP)
    ]
    (train_dir / "history.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )

    rc = cli.cmd_stats(argparse.Namespace(repo=repo, days=1))

    assert rc == 0
    assert f"at least {cli.JSONL_TAIL_CAP} records" in capsys.readouterr().out


def test_stats_omits_the_cap_note_when_history_is_small(tmp_path, capsys):
    sys.path.insert(0, str(CLI_SCRIPT.parent))
    import argparse

    import cli

    repo = tmp_path / "repo"
    train_dir = repo / ".train"
    train_dir.mkdir(parents=True)
    entry = {"branch": "claude/x", "status": "landed", "ts": time.time()}
    (train_dir / "history.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    rc = cli.cmd_stats(argparse.Namespace(repo=repo, days=1))

    assert rc == 0
    assert "records" not in capsys.readouterr().out


# --- switchyard radar / land (thin passthroughs) ------------------------------


def test_radar_passthrough_reports_conflicts_json(tmp_path):
    origin, station = make_world(tmp_path)

    proc = run_cli("radar", "--repo", str(station), "--json", home=tmp_path)

    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    names = {r["a"] for r in results} | {r["b"] for r in results}
    assert "claude/good" in names
    assert "claude/bad" in names


def test_land_passthrough_lands_branch(tmp_path):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)

    proc = run_cli(
        "land",
        "--repo",
        str(station),
        "--branch",
        "claude/good",
        "--gate",
        "/usr/bin/true",
        home=tmp_path,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert origin_main(origin) != before
    assert git(origin, "show", "main:good.txt") == "good change"
