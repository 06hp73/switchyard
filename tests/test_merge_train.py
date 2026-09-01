"""The train lands green branches serially and never advances main on red.

Uses a real local git topology: a bare 'origin', a 'station' clone the train
operates, and feature branches pushed to origin. The gate command is injected
(true/false stand-ins), so no project tests run here.
"""

import json
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
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=ENV,
    )
    return proc.stdout.strip()


def make_world(tmp_path: Path, protected: str = "main") -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", protected)
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(origin), str(seed)],
        check=True,
        capture_output=True,
        env=ENV,
    )
    (seed / "app.txt").write_text("v1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "origin", protected)
    for name, content in [
        ("claude/good", "good change\n"),
        ("claude/bad", "bad change\n"),
    ]:
        git(seed, "checkout", "-b", name, protected)
        (seed / f"{name.split('/')[1]}.txt").write_text(content)
        git(seed, "add", "-A")
        git(seed, "commit", "-m", name)
        git(seed, "push", "origin", name)
        git(seed, "checkout", protected)
    station = tmp_path / "station"
    subprocess.run(
        ["git", "clone", str(origin), str(station)],
        check=True,
        capture_output=True,
        env=ENV,
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
        repo=station,
        branches=["claude/good"],
        gate=["/bin/sh", "-c", "sleep 3"],
        gate_timeout=1,
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


# --- lock mutual exclusion (C2: the pid file itself is the mutex) -----------


def test_acquire_lock_live_pid_refuses(tmp_path):
    from merge_train import _acquire_lock

    lock_dir = tmp_path / ".train" / "lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text(str(os.getpid()))

    with pytest.raises(SystemExit):
        _acquire_lock(tmp_path)


def test_acquire_lock_dead_pid_reclaims_and_proceeds(tmp_path):
    from merge_train import _acquire_lock

    lock_dir = tmp_path / ".train" / "lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text("99999999")

    lock = _acquire_lock(tmp_path)

    assert lock == lock_dir
    assert int((lock_dir / "pid").read_text().strip()) == os.getpid()


def test_acquire_lock_is_mutually_exclusive_until_released(tmp_path):
    # The pid file itself is the mutex (an O_EXCL atomic create), not just
    # the containing directory existing: two sequential acquires on the same
    # repo with no release in between must refuse the second - proving real
    # exclusion, not just "a directory happens to be there already".
    from merge_train import _acquire_lock, _release_lock

    lock = _acquire_lock(tmp_path)

    with pytest.raises(SystemExit):
        _acquire_lock(tmp_path)

    _release_lock(lock)

    # Released: a subsequent acquire must succeed again.
    lock2 = _acquire_lock(tmp_path)
    assert lock2 == lock
    _release_lock(lock2)


def test_acquire_lock_reclaim_race_loss_is_treated_as_held(tmp_path, monkeypatch):
    # Simulates the exact race the atomic-pidfile fix exists to close: a
    # dead holder is found (eligible for reclaim), but between our unlink of
    # its stale pidfile and our own retry create, another reclaimer wins
    # first. Our retry's O_EXCL create must then lose for real (not silently
    # succeed and hand out a second lock) and we must refuse cleanly - never
    # an uncaught FileExistsError bubbling out of a "concurrent reclaimers"
    # race.
    from merge_train import _acquire_lock

    lock_dir = tmp_path / ".train" / "lock"
    lock_dir.mkdir(parents=True)
    pid_file = lock_dir / "pid"
    pid_file.write_text("99999999")  # dead - eligible for reclaim

    real_open = os.open
    target = str(pid_file)
    calls = {"n": 0}

    def racy_open(path, *args, **kwargs):
        if str(path) == target:
            calls["n"] += 1
            if calls["n"] == 2:
                # A concurrent reclaimer wins the race right before our own
                # retry create runs - it should see this and lose too.
                Path(path).write_text(str(os.getpid()))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", racy_open)

    with pytest.raises(SystemExit):
        _acquire_lock(tmp_path)


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


FAKE_GH_BODY = """# Stub `gh` for the pr-squash landing tests - see write_fake_gh() below.
import json
import os
import subprocess
import sys
import tempfile


def main():
    args = sys.argv[1:]
    argv_log = os.environ.get("FAKE_GH_ARGV_LOG")
    if argv_log:
        with open(argv_log, "a") as f:
            f.write(json.dumps(args) + "\\n")
    if args[:2] == ["pr", "list"]:
        pr_number = os.environ.get("FAKE_GH_PR_NUMBER", "")
        if pr_number:
            print(pr_number)
        return 0
    if args[:2] == ["pr", "merge"]:
        mode = os.environ.get("FAKE_GH_MERGE_MODE", "ok")
        if mode == "fail":
            sys.stderr.write(os.environ.get("FAKE_GH_FAIL_MESSAGE", "merge blocked") + "\\n")
            return 1
        if mode == "head_moved":
            # Simulates GitHub's real rejection when --match-head-commit no
            # longer matches the server-side head (e.g. main's required-checks
            # branch moved after the train's local gate approved this SHA).
            if "--match-head-commit" not in args:
                sys.stderr.write("fake_gh stub: head_moved mode expects --match-head-commit\\n")
                return 1
            sys.stderr.write("Head branch was modified. Review and try the merge again.\\n")
            return 1
        branch = os.environ["FAKE_GH_BRANCH"]
        origin_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        clone = tempfile.mkdtemp(prefix="fake-gh-clone-")
        subprocess.run(["git", "clone", "-q", origin_url, clone], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", clone, "merge", "--squash", "origin/" + branch],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", clone, "commit", "-m", "squash merge " + branch],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", clone, "push", "-q", "origin", "main"], check=True, capture_output=True
        )
        return 0
    sys.stderr.write("fake_gh stub: unhandled args " + repr(args) + "\\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


def write_fake_gh(tmp_path: Path) -> Path:
    """Write the stub `gh` binary the pr-squash tests inject via SWITCHYARD_GH.

    There is no live GitHub in these tests, so the mechanism is simplified:
    real `gh pr merge --squash` lands the PR server-side via GitHub's API; the
    stub instead clones the test's own bare `origin` fresh (cwd is the
    station repo, so plain `git remote get-url origin` finds it), does a
    plain `git merge --squash` of the target branch (named by $FAKE_GH_BRANCH
    - a stand-in for what a real PR number would resolve to server-side) onto
    that clone's main, commits, and pushes straight to origin/main. Base and
    branch diff are identical to what process_branch's local --no-ff merge
    already validated and there is no textual conflict in these fixtures, so
    the resulting tree is bit-identical to the validated one - exactly the
    invariant _land_via_pr_squash's post-merge tree check depends on.
    """
    script = tmp_path / "fake_gh.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_GH_BODY)
    script.chmod(0o755)
    return script


FAKE_GH_MULTI_BODY = """# Stub `gh` for the batch pr-squash test - see write_fake_gh_multi() below.
import json
import os
import subprocess
import sys
import tempfile


def main():
    args = sys.argv[1:]
    argv_log = os.environ.get("FAKE_GH_ARGV_LOG")
    if argv_log:
        with open(argv_log, "a") as f:
            f.write(json.dumps(args) + "\\n")
    branch_prs = json.loads(os.environ["FAKE_GH_BRANCH_PRS"])  # {branch: pr_number}
    pr_branches = {v: k for k, v in branch_prs.items()}
    if args[:2] == ["pr", "list"]:
        branch = args[args.index("--head") + 1]
        pr_number = branch_prs.get(branch, "")
        if pr_number:
            print(pr_number)
        return 0
    if args[:2] == ["pr", "merge"]:
        pr_number = args[2]
        branch = pr_branches[pr_number]
        origin_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        clone = tempfile.mkdtemp(prefix="fake-gh-multi-clone-")
        subprocess.run(["git", "clone", "-q", origin_url, clone], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", clone, "merge", "--squash", "origin/" + branch],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", clone, "commit", "-m", "squash merge " + branch],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", clone, "push", "-q", "origin", "main"], check=True, capture_output=True
        )
        return 0
    sys.stderr.write("fake_gh_multi stub: unhandled args " + repr(args) + "\\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


def write_fake_gh_multi(tmp_path: Path) -> Path:
    """Write the stub `gh` for the batch pr-squash test.

    write_fake_gh's stub always operates on one hardcoded FAKE_GH_BRANCH, but
    a batch lands several different branches through the same `gh` binary in
    one run - this stub instead takes a real branch<->PR-number mapping
    (FAKE_GH_BRANCH_PRS, a JSON object) so it can resolve which branch a
    given `pr list --head <branch>` or `pr merge <number>` call is actually
    about, the same way the real `gh` would via GitHub's own PR database.
    """
    script = tmp_path / "fake_gh_multi.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_GH_MULTI_BODY)
    script.chmod(0o755)
    return script


def test_pr_squash_lands_and_tree_matches(tmp_path, monkeypatch):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    fake_gh = write_fake_gh(tmp_path)
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_PR_NUMBER", "1")
    monkeypatch.setenv("FAKE_GH_MERGE_MODE", "ok")
    monkeypatch.setenv("FAKE_GH_BRANCH", "claude/good")

    results = run_train(
        repo=station, branches=["claude/good"], gate=["/usr/bin/true"], land="pr-squash"
    )

    assert results == [TrainResult(branch="claude/good", status="landed")]
    assert origin_main(origin) != before
    # Independent content check that the tree-verification is real, not a
    # tautology: the squash landed through gh must carry both the unchanged
    # base file and the branch's new file, byte for byte.
    assert (station / "app.txt").read_text() == "v1\n"
    assert (station / "good.txt").read_text() == "good change\n"


def test_pr_squash_merge_rejected_leaves_main_unmoved(tmp_path, monkeypatch):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    fake_gh = write_fake_gh(tmp_path)
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_PR_NUMBER", "7")
    monkeypatch.setenv("FAKE_GH_MERGE_MODE", "fail")
    monkeypatch.setenv("FAKE_GH_FAIL_MESSAGE", "required status checks have not been met")

    results = run_train(
        repo=station, branches=["claude/good"], gate=["/usr/bin/true"], land="pr-squash"
    )

    assert results[0].status == "rejected"
    assert "required status checks" in results[0].detail
    assert origin_main(origin) == before


def test_pr_squash_no_open_pr_is_error(tmp_path, monkeypatch):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    fake_gh = write_fake_gh(tmp_path)
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_PR_NUMBER", "")  # stub's `pr list` prints nothing back

    results = run_train(
        repo=station, branches=["claude/good"], gate=["/usr/bin/true"], land="pr-squash"
    )

    assert results[0].status == "error"
    assert "no open PR" in results[0].detail
    assert origin_main(origin) == before


def test_pr_squash_tree_mismatch_is_loud_error(tmp_path, monkeypatch):
    # Simulates gh landing something other than what was gate-tested (e.g.
    # main moved between the local test and the real landing): point the
    # stub at the WRONG branch, so what actually lands on origin/main cannot
    # match the tree process_branch validated for "claude/good".
    origin, station = make_world(tmp_path)
    fake_gh = write_fake_gh(tmp_path)
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_PR_NUMBER", "1")
    monkeypatch.setenv("FAKE_GH_MERGE_MODE", "ok")
    monkeypatch.setenv("FAKE_GH_BRANCH", "claude/bad")

    results = run_train(
        repo=station, branches=["claude/good"], gate=["/usr/bin/true"], land="pr-squash"
    )

    assert results[0].status == "error"
    assert "tree mismatch" in results[0].detail


def test_pr_squash_passes_match_head_commit_with_correct_sha(tmp_path, monkeypatch):
    # Closes the approved-then-moved hole: gh must be told the exact SHA the
    # local gate approved, so GitHub itself refuses the squash server-side if
    # the branch's head moved after gating (rather than trusting our stale
    # local read of it).
    origin, station = make_world(tmp_path)
    branch_sha = git(origin, "rev-parse", "claude/good")
    fake_gh = write_fake_gh(tmp_path)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_PR_NUMBER", "1")
    monkeypatch.setenv("FAKE_GH_MERGE_MODE", "ok")
    monkeypatch.setenv("FAKE_GH_BRANCH", "claude/good")
    monkeypatch.setenv("FAKE_GH_ARGV_LOG", str(argv_log))

    results = run_train(
        repo=station, branches=["claude/good"], gate=["/usr/bin/true"], land="pr-squash"
    )

    assert results == [TrainResult(branch="claude/good", status="landed")]
    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    merge_calls = [c for c in calls if c[:2] == ["pr", "merge"]]
    assert len(merge_calls) == 1
    assert "--match-head-commit" in merge_calls[0]
    sha_index = merge_calls[0].index("--match-head-commit") + 1
    assert merge_calls[0][sha_index] == branch_sha


def test_pr_squash_head_moved_after_gating_is_rejected(tmp_path, monkeypatch):
    # Simulates GitHub refusing the squash server-side because the branch's
    # head moved after the local gate approved a now-stale SHA - exactly the
    # approved-then-moved race --match-head-commit exists to close.
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    fake_gh = write_fake_gh(tmp_path)
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_PR_NUMBER", "3")
    monkeypatch.setenv("FAKE_GH_MERGE_MODE", "head_moved")

    results = run_train(
        repo=station, branches=["claude/good"], gate=["/usr/bin/true"], land="pr-squash"
    )

    assert results[0].status == "rejected"
    assert results[0].detail == "head moved after gating - re-queue"
    assert origin_main(origin) == before


def test_cli_dry_run_red_exits_1(tmp_path):
    origin, station = make_world(tmp_path)
    cli = [sys.executable, str(MERGE_TRAIN_SCRIPT), "run", "--repo", str(station)]

    dry_run = subprocess.run(
        [*cli, "--branch", "claude/bad", "--gate", "/usr/bin/false", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_run.returncode == 1, dry_run.stderr

    # Without --dry-run, a rejected branch is a normal outcome: exit 0.
    real_run = subprocess.run(
        [*cli, "--branch", "claude/bad", "--gate", "/usr/bin/false"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert real_run.returncode == 0, real_run.stderr


def test_batch_one_is_byte_equivalent_to_unbatched(tmp_path):
    # Same scenario as test_green_branch_lands, with batch=1 spelled out
    # explicitly: the batch parameter must not change the plain path at all.
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    results = run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"], batch=1)
    assert results == [TrainResult(branch="claude/good", status="landed")]
    assert origin_main(origin) != before


def test_batch_green_lands_all_with_one_gate_run(tmp_path):
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    gate = ["/bin/sh", "-c", f"echo x >> {counter}; exit 0"]

    results = run_train(repo=station, branches=["claude/good", "claude/bad"], gate=gate, batch=2)

    assert [r.status for r in results] == ["landed", "landed"]
    assert len(counter.read_text().splitlines()) == 1
    # Both branches' content changes really landed on origin/main, not just
    # one of them or a tree that happens to satisfy the gate by luck.
    assert git(origin, "show", "main:good.txt") == "good change"
    assert git(origin, "show", "main:bad.txt") == "bad change"


def test_batch_red_bisects_to_culprit(tmp_path):
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    # Fails whenever claude/bad's file is present in the candidate tree -
    # a stand-in for "this branch breaks the gate", independent of claude/good.
    gate = [
        "/bin/sh",
        "-c",
        f"echo x >> {counter}; test -f bad.txt && exit 1 || exit 0",
    ]

    results = run_train(repo=station, branches=["claude/good", "claude/bad"], gate=gate, batch=2)

    assert [r.status for r in results] == ["landed", "rejected"]
    # Three gate runs: the full pair (red), then each half alone (AB, A, B) -
    # bisection, not a re-run of the same failing gate.
    assert len(counter.read_text().splitlines()) == 3
    assert git(origin, "show", "main:good.txt") == "good change"
    with pytest.raises(subprocess.CalledProcessError):
        git(origin, "show", "main:bad.txt")


def test_batch_conflicting_member_set_aside(tmp_path):
    origin, station = make_world(tmp_path)
    seed = tmp_path / "seed"
    git(seed, "checkout", "main")
    git(seed, "checkout", "-b", "claude/collide", "main")
    (seed / "good.txt").write_text("collide change\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "collide with claude/good's good.txt")
    git(seed, "push", "origin", "claude/collide")
    git(seed, "checkout", "main")

    counter = tmp_path / "count"
    gate = ["/bin/sh", "-c", f"echo x >> {counter}; exit 0"]

    results = run_train(
        repo=station, branches=["claude/good", "claude/collide"], gate=gate, batch=2
    )

    assert [r.status for r in results] == ["landed", "conflict"]
    # The conflicting member never made it into the candidate, so it never
    # cost a gate run of its own - one candidate (claude/good alone) built.
    assert len(counter.read_text().splitlines()) == 1
    assert git(origin, "show", "main:good.txt") == "good change"


def test_batch_prsquash_final_tree_verified(tmp_path, monkeypatch):
    origin, station = make_world(tmp_path)
    fake_gh = write_fake_gh_multi(tmp_path)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("SWITCHYARD_GH", str(fake_gh))
    monkeypatch.setenv("FAKE_GH_BRANCH_PRS", json.dumps({"claude/good": "1", "claude/bad": "2"}))
    monkeypatch.setenv("FAKE_GH_ARGV_LOG", str(argv_log))

    results = run_train(
        repo=station,
        branches=["claude/good", "claude/bad"],
        gate=["/usr/bin/true"],
        land="pr-squash",
        batch=2,
    )

    assert [r.status for r in results] == ["landed", "landed"]
    # No mismatch error: origin/main's tree after both gh squashes lands
    # really does equal the tree the train gated once, up front.
    assert git(origin, "show", "main:good.txt") == "good change"
    assert git(origin, "show", "main:bad.txt") == "bad change"

    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    merge_calls = [c for c in calls if c[:2] == ["pr", "merge"]]
    assert len(merge_calls) == 2
    branch_head_sha = {
        "claude/good": git(origin, "rev-parse", "claude/good"),
        "claude/bad": git(origin, "rev-parse", "claude/bad"),
    }
    pr_to_branch = {"1": "claude/good", "2": "claude/bad"}
    for call in merge_calls:
        assert "--match-head-commit" in call
        sha_index = call.index("--match-head-commit") + 1
        assert call[sha_index] == branch_head_sha[pr_to_branch[call[2]]]


# --- switchyard.toml config threading ---------------------------------------


def test_pr_sort_key_prioritizes_priority_label_then_number():
    from merge_train import _pr_sort_key

    prs = [
        {"number": 5, "headRefName": "b", "labels": []},
        {"number": 2, "headRefName": "a", "labels": [{"name": "train-priority"}]},
        {"number": 1, "headRefName": "c", "labels": []},
    ]
    ordered = sorted(prs, key=lambda p: _pr_sort_key(p, "train-priority"))
    assert [p["headRefName"] for p in ordered] == ["a", "c", "b"]


def test_protected_branch_param_used_when_default_branch_is_not_main(tmp_path):
    # run_train's protected="main" default is only a fallback for callers that
    # never touch config; passing a different name must retarget every "main"
    # the train would otherwise hardcode (checkout, reset, push, merge-tree
    # base) - proven here with a world whose default branch is "trunk", not
    # "main", which would fail fast (checkout of a nonexistent local "main")
    # if any hardcoded literal survived the refactor.
    origin, station = make_world(tmp_path, protected="trunk")
    before = git(origin, "rev-parse", "trunk")

    results = run_train(
        repo=station,
        branches=["claude/good"],
        gate=["/usr/bin/true"],
        protected="trunk",
    )

    assert results == [TrainResult(branch="claude/good", status="landed")]
    assert git(origin, "rev-parse", "trunk") != before
    assert git(origin, "show", "trunk:good.txt") == "good change"


def test_train_respects_config_gate_fast_and_batch_via_switchyard_toml(tmp_path):
    # main() is the only layer that reads config (run_train itself stays
    # config-agnostic, see the module docstring) - so this drives the real
    # CLI as a subprocess. Neither --gate nor --batch is passed: if config
    # threading were broken, --gate would fall back to GATE_DEFAULT (bash
    # tools/train/gate.sh, which does not exist in this throwaway station
    # clone) and batch would stay 1, landing one branch per gate run instead
    # of both branches through a single configured gate.
    origin, station = make_world(tmp_path)
    config = tmp_path / "switchyard.toml"
    config.write_text('[switchyard]\ngate_fast = "/usr/bin/true"\nbatch = 2\n')

    cli = [
        sys.executable,
        str(MERGE_TRAIN_SCRIPT),
        "run",
        "--repo",
        str(station),
        "--branch",
        "claude/good",
        "--branch",
        "claude/bad",
    ]
    proc = subprocess.run(
        cli,
        capture_output=True,
        text=True,
        env={**os.environ, "SWITCHYARD_CONFIG": str(config)},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert git(origin, "show", "main:good.txt") == "good change"
    assert git(origin, "show", "main:bad.txt") == "bad change"


# --- landing history (.train/history.jsonl) ---------------------------------


def test_history_appends_line_on_green_land(tmp_path):
    origin, station = make_world(tmp_path)
    results = run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"])
    assert results[0].status == "landed"

    history = station / ".train" / "history.jsonl"
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["branch"] == "claude/good"
    assert entry["status"] == "landed"
    assert entry["gate_seconds"] > 0.0
    assert entry["tree"]  # non-empty candidate tree hash
    assert entry["batch"] == 1
    assert isinstance(entry["ts"], float)
    assert entry["detail_first_line"] == ""


def test_history_cache_hit_records_zero_gate_seconds(tmp_path):
    origin, station = make_world(tmp_path)
    gate = ["/usr/bin/true"]
    run_train(repo=station, branches=["claude/good"], gate=gate)
    # Re-run the identical already-landed branch against the same gate: the
    # merge is a no-op (already up to date) so the candidate tree - and hence
    # the cache key - is unchanged, and the gate must not actually run again.
    run_train(repo=station, branches=["claude/good"], gate=gate)

    history = station / ".train" / "history.jsonl"
    entries = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 2
    assert entries[0]["gate_seconds"] > 0.0
    assert entries[1]["gate_seconds"] == 0.0
    assert entries[1]["status"] == "landed"


def test_corrupt_history_dir_does_not_affect_results(tmp_path, capsys):
    origin, station = make_world(tmp_path)
    train_dir = station / ".train"
    train_dir.mkdir(parents=True, exist_ok=True)
    # history.jsonl exists as a directory, not a file: open(..., "a") must
    # raise OSError, and that must never surface as a broken landing.
    (train_dir / "history.jsonl").mkdir()

    results = run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"])

    assert results == [TrainResult(branch="claude/good", status="landed")]
    assert (train_dir / "history.jsonl").is_dir()  # untouched, still a directory
    out = capsys.readouterr().out
    assert "history" in out.lower()


# --- retry-once + flaky quarantine register (process_branch only) -----------
#
# retry_flaky defaults to False at the run_train()/process_branch() level (see
# the module docstring's "Config" paragraph) so every test above this line -
# every existing caller that predates this feature - keeps exercising the
# gate exactly once. These tests opt in explicitly with retry_flaky=True,
# same as they already do for batch=2, land="pr-squash", etc.


def test_retry_flaky_rescues_a_failing_gate_and_logs_it(tmp_path, capsys):
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    # Fails the first time it is ever invoked (counter file does not exist
    # yet), passes every time after - a one-off flake, not a reproducible
    # failure. `test -f` + a trailing append is the whole state machine.
    gate = [
        "/bin/sh",
        "-c",
        f"test -f {counter} && ec=0 || ec=1; echo x >> {counter}; exit $ec",
    ]

    results = run_train(repo=station, branches=["claude/good"], gate=gate, retry_flaky=True)

    assert results[0].status == "landed"
    assert results[0].flaky is True
    assert len(counter.read_text().splitlines()) == 2  # first run + the retry

    out = capsys.readouterr().out
    assert "FLAKY GATE: landed on retry - signal preserved in flaky_log" in out

    flaky_log = station / ".train" / "flaky_log.jsonl"
    lines = flaky_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["gate"] == " ".join(gate)
    assert entry["tree"]  # non-empty candidate tree hash
    assert "gate failed" in entry["first_tail"]
    assert isinstance(entry["ts"], float)


def test_retry_flaky_fail_fail_is_rejected_with_both_tails(tmp_path):
    origin, station = make_world(tmp_path)
    before = origin_main(origin)
    counter = tmp_path / "count"
    gate = ["/bin/sh", "-c", f"echo x >> {counter}; exit 1"]  # deterministic, always red

    results = run_train(repo=station, branches=["claude/good"], gate=gate, retry_flaky=True)

    assert results[0].status == "rejected"
    assert results[0].flaky is False
    assert len(counter.read_text().splitlines()) == 2  # first run + the retry
    detail_lower = results[0].detail.lower()
    assert "first run" in detail_lower
    assert "retry run" in detail_lower
    assert origin_main(origin) == before

    flaky_log = station / ".train" / "flaky_log.jsonl"
    assert not flaky_log.exists()  # never rescued - nothing to log


def test_no_retry_flaky_cli_flag_runs_gate_only_once(tmp_path):
    # retry_flaky is only ever reached through main()/config or an explicit
    # run_train(retry_flaky=True) - this drives the real CLI to prove
    # --no-retry-flaky actually overrides a config that turns it on.
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    gate_script = tmp_path / "gate.sh"
    gate_script.write_text(f"#!/bin/sh\necho x >> {counter}\nexit 1\n")
    gate_script.chmod(0o755)
    config = tmp_path / "switchyard.toml"
    config.write_text("[switchyard]\nretry_flaky = true\n")

    cli = [
        sys.executable,
        str(MERGE_TRAIN_SCRIPT),
        "run",
        "--repo",
        str(station),
        "--branch",
        "claude/good",
        "--gate",
        str(gate_script),
        "--no-retry-flaky",
    ]
    proc = subprocess.run(
        cli,
        capture_output=True,
        text=True,
        env={**os.environ, "SWITCHYARD_CONFIG": str(config)},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr  # a rejected branch is a normal outcome
    assert len(counter.read_text().splitlines()) == 1


def test_timeout_is_never_retried_even_with_retry_flaky_on(tmp_path):
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    gate = ["/bin/sh", "-c", f"echo x >> {counter}; sleep 3"]

    results = run_train(
        repo=station,
        branches=["claude/good"],
        gate=gate,
        gate_timeout=1,
        retry_flaky=True,
    )

    assert results[0].status == "rejected"
    assert "timed out" in results[0].detail
    assert len(counter.read_text().splitlines()) == 1  # never retried


def test_batch_red_bisection_count_unaffected_by_retry_flaky(tmp_path):
    # retry_flaky is process_branch-only (see _run_gate_with_retry's
    # docstring) - passing it through run_train while batch=2 must not touch
    # _run_batch's gate-run accounting at all, since run_train never forwards
    # retry_flaky into _run_batch/_run_gate in the first place.
    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    gate = [
        "/bin/sh",
        "-c",
        f"echo x >> {counter}; test -f bad.txt && exit 1 || exit 0",
    ]

    results = run_train(
        repo=station, branches=["claude/good", "claude/bad"], gate=gate, batch=2, retry_flaky=True
    )

    assert [r.status for r in results] == ["landed", "rejected"]
    assert len(counter.read_text().splitlines()) == 3  # AB, A, B - unchanged from batch=2 alone


# --- notifications (opt-in, cfg.notify -> run_train(notify_mode=...)) -------
#
# tools/lib/notify.py's own backend behavior (osascript argv, "none" firing
# no subprocess at all) is covered in tests/test_notify.py; these tests only
# check that run_train actually calls it, for the right outcomes, with the
# right mode - by monkeypatching the module-level _send_notification name
# merge_train.py's _notify_result calls, never the real notify() backend.


def test_run_train_notifies_on_landed_and_rejected(tmp_path, monkeypatch):
    import merge_train

    calls = []
    monkeypatch.setattr(
        merge_train,
        "_send_notification",
        lambda title, message, cfg: calls.append((title, message, cfg.notify)),
    )

    origin, station = make_world(tmp_path)
    run_train(
        repo=station,
        branches=["claude/bad", "claude/good"],
        gate=None,
        gate_factory=lambda branch: (
            ["/usr/bin/false"] if branch == "claude/bad" else ["/usr/bin/true"]
        ),
        notify_mode="macos",
    )

    assert len(calls) == 2
    rejected_title, rejected_message, rejected_mode = calls[0]
    landed_title, landed_message, landed_mode = calls[1]
    assert rejected_mode == landed_mode == "macos"
    assert "rejected" in (rejected_title + rejected_message).lower()
    assert "landed" in (landed_title + landed_message).lower()


def test_run_train_notifies_on_error(tmp_path, monkeypatch):
    import merge_train

    calls = []
    monkeypatch.setattr(
        merge_train, "_send_notification", lambda title, message, cfg: calls.append(title)
    )

    origin, station = make_world(tmp_path)
    run_train(repo=station, branches=["claude/ghost"], gate=["/usr/bin/true"], notify_mode="macos")

    assert len(calls) == 1
    assert "error" in calls[0].lower()


def test_run_train_does_not_notify_on_conflict(tmp_path, monkeypatch):
    import merge_train

    calls = []
    monkeypatch.setattr(
        merge_train, "_send_notification", lambda title, message, cfg: calls.append(title)
    )

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

    run_train(repo=station, branches=["claude/clash"], gate=["/usr/bin/true"], notify_mode="macos")

    assert calls == []


def test_run_train_flaky_landed_gets_a_distinct_notification(tmp_path, monkeypatch):
    import merge_train

    calls = []
    monkeypatch.setattr(
        merge_train,
        "_send_notification",
        lambda title, message, cfg: calls.append(title + " " + message),
    )

    origin, station = make_world(tmp_path)
    counter = tmp_path / "count"
    gate = [
        "/bin/sh",
        "-c",
        f"test -f {counter} && ec=0 || ec=1; echo x >> {counter}; exit $ec",
    ]

    run_train(
        repo=station,
        branches=["claude/good"],
        gate=gate,
        retry_flaky=True,
        notify_mode="macos",
    )

    assert len(calls) == 1
    assert "flaky" in calls[0].lower()


def test_default_notify_mode_is_none(tmp_path, monkeypatch):
    # run_train()'s own default (notify_mode="none") must be a no-op for
    # every caller that predates this feature. test_notify.py separately
    # proves notify() itself never shells out for "none" - this proves the
    # default actually reaches it, unchanged, end to end.
    import merge_train

    calls = []
    monkeypatch.setattr(
        merge_train, "_send_notification", lambda title, message, cfg: calls.append(cfg.notify)
    )

    origin, station = make_world(tmp_path)
    run_train(repo=station, branches=["claude/good"], gate=["/usr/bin/true"])

    assert calls == ["none"]
