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


def run_cli(*args: str, home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**ENV, "HOME": str(home)},
        timeout=timeout,
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
