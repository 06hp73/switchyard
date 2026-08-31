"""The train lands green branches serially and never advances main on red.

Uses a real local git topology: a bare 'origin', a 'station' clone the train
operates, and feature branches pushed to origin. The gate command is injected
(true/false stand-ins), so no project tests run here.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "train"))
MERGE_TRAIN_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "train" / "merge_train.py"

from merge_train import TrainResult, _cache_key, run_train  # noqa: E402

ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=ENV
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
    return origin, station


def origin_main(origin: Path) -> str:
    return git(origin, "rev-parse", "main")


def test_green_branch_lands(tmp_path):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    results = run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"])
    assert results == [TrainResult(branch="claude/good", status="landed")]
    assert origin_main(origin) != before


def test_red_branch_never_touches_main(tmp_path):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    results = run_train(repo=station, branches=["claude/bad"], gate=["/usr/bin/false"])
    assert results[0].status == "rejected"
    assert origin_main(origin) == before


def test_red_does_not_block_the_next_green(tmp_path):
    origin, station = make_world(tmp_path)
    results = run_train(
        repo=station,
        branches=["claude/bad", "claude/good"],
        gate=None,
        gate_factory=lambda branch: (
            ["/usr/bin/false"] if branch == "claude/bad" else ["/usr/bin/true"]
        ),
    )
    assert [r.status for r in results] == ["rejected", "landed"]


def test_tree_cache_skips_revalidation(tmp_path):
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    gate = ["/bin/sh", "-c", f"echo x >> {counter}; exit 0"]
    run_train(repo=station, branches=["claude/good"], gate=gate)
    runs_first = len(counter.read_text().splitlines())
    # Re-run with the same already-landed branch: nothing to do, gate not re-run.
    run_train(repo=station, branches=["claude/good"], gate=gate)
    assert len(counter.read_text().splitlines()) == runs_first


def test_textual_conflict_is_rejected_without_gate(tmp_path):
    origin, station = make_world(tmp_path)
    seed = tmp_path / "seed"
    git(seed, "checkout", "main")
    (seed / "app.txt").write_text("main moved\n")
    git(seed, "commit", "-am", "main change")
    git(seed, "push", "origin", "main")
    git(seed, "checkout", "-b", "claude/clash", "HEAD~1")
    (seed / "app.txt").write_text("branch clash\n")
    git(seed, "commit", "-am", "clash")
    git(seed, "push", "origin", "claude/clash")
    results = run_train(repo=station, branches=["claude/clash"], gate=["/usr/bin/true"])
    assert results[0].status == "conflict"


def test_cache_is_gate_specific(tmp_path):
    # A dry-run with a cheap gate must not pre-approve the tree for a later
    # real run with a different (or any) gate: the false gate must actually run.
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    counter = tmp_path / "false_count"
    false_gate = ["/bin/sh", "-c", f"echo x >> {counter}; exit 1"]
    run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"], dry_run=True)
    results = run_train(repo=station, branches=["claude/good"], gate=false_gate)
    assert results[0].status == "rejected"
    assert counter.exists() and len(counter.read_text().splitlines()) == 1
    assert origin_main(origin) == before


def test_gate_timeout_rejects(tmp_path):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    results = run_train(
        repo=station, branches=["claude/good"], gate=["/bin/sh", "-c", "sleep 3"], gate_timeout=1
    )
    assert results[0].status == "rejected"
    assert "timed out" in results[0].detail
    assert origin_main(origin) == before


def test_stale_lock_reclaimed(tmp_path):
    origin, station = make_world(tmp_path)
    lock = station / ".train" / "lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("99999999")
    results = run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"])
    assert results[0].status == "landed"


def test_live_lock_refuses(tmp_path):
    origin, station = make_world(tmp_path)
    lock = station / ".train" / "lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    with pytest.raises(SystemExit):
        run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"])


def test_missing_gate_binary_is_error_and_queue_continues(tmp_path):
    origin, station = make_world(tmp_path)
    results = run_train(
        repo=station,
        branches=["claude/bad", "claude/good"],
        gate=None,
        gate_factory=lambda branch: (
            ["/nonexistent/gate-binary"] if branch == "claude/bad" else ["/usr/bin/true"]
        ),
    )
    assert [r.status for r in results] == ["error", "landed"]


def test_ghost_branch_reports_error(tmp_path):
    origin, station = make_world(tmp_path)
    results = run_train(repo=station, branches=["claude/ghost"], gate=["/usr/bin/true"])
    assert results[0].status == "error"
    assert "not found" in results[0].detail


def test_gate_timeout_kills_process_tree(tmp_path):
    # A gate that shells out (bash -> pytest) makes the real work a
    # grandchild of the gate command. subprocess.run's timeout= only kills
    # the direct child; this proves the whole process GROUP dies instead, by
    # backgrounding a long sleep, recording its pid, then waiting on it.
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    sleeper_pid_file = tmp_path / "sleeper_pid"
    gate = ["/bin/sh", "-c", f"(sleep 30 & echo $! > {sleeper_pid_file}; wait)"]

    results = run_train(repo=station, branches=["claude/good"], gate=gate, gate_timeout=1)

    assert results[0].status == "rejected"
    assert origin_main(origin) == before

    pid = int(sleeper_pid_file.read_text().strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"sleeper pid {pid} still alive 3s after gate timeout")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cache_key_argv_boundaries():
    # A space-joined cache key would let ["a b", "c"] and ["a", "b c"]
    # collide onto the same gate identity - different commands, same key.
    assert _cache_key("t", ["a b", "c"]) != _cache_key("t", ["a", "b c"])


def test_corrupt_cache_quarantined_then_selfheals(tmp_path):
    origin, station = make_world(tmp_path)
    train_dir = station / ".train"
    train_dir.mkdir(parents=True, exist_ok=True)
    cache_file = train_dir / "validated_trees.txt"
    cache_file.write_bytes(b"\xff\xfe\x00garbage")

    counter = tmp_path / "count"
    gate = ["/bin/sh", "-c", f"echo x >> {counter}; exit 0"]

    results = run_train(repo=station, branches=["claude/good"], gate=gate)
    assert results[0].status == "landed"
    assert len(counter.read_text().splitlines()) == 1
    assert (train_dir / "validated_trees.corrupt").exists()

    # Cache self-healed on the first successful land: re-running the same
    # branch against the same gate must not re-run the gate.
    results = run_train(repo=station, branches=["claude/good"], gate=gate)
    assert results[0].status == "landed"
    assert len(counter.read_text().splitlines()) == 1


def test_cli_dry_run_red_exits_1(tmp_path):
    origin, station = make_world(tmp_path)
    cli = [sys.executable, str(MERGE_TRAIN_SCRIPT), "run", "--repo", str(station)]

    dry_run = subprocess.run(
        [*cli, "--branch", "claude/bad", "--gate", "/usr/bin/false", "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert dry_run.returncode == 1, dry_run.stderr

    # Without --dry-run, a rejected branch is a normal outcome: exit 0.
    real_run = subprocess.run(
        [*cli, "--branch", "claude/bad", "--gate", "/usr/bin/false"],
        capture_output=True,
        text=True,
    )
    assert real_run.returncode == 0, real_run.stderr
