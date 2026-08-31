"""The merge train: the only writer to main.

One branch at a time: refresh main, replay-check, build the real merge, test
the COMBINED tree, push only on green. A red branch is reported and skipped -
it never blocks the branches behind it, and it never moves main.

The train runs as a normal user process (never through a Claude session's
Bash tool), so the session guard hooks do not apply to it - by design: the
guards ban sessions from pushing main precisely because the train does it.

State (at the repo root):
    .train/validated_trees.txt  - cache keys (tree hash + gate argv, hashed)
                                  whose gate already passed
    .train/lock/                - mkdir-mutex (with pid file) so only one
                                  train runs; stale locks are reclaimed

Usage:
    python tools/train/merge_train.py run [--repo PATH] [--branch NAME ...]
        [--gate CMD] [--gate-timeout SECONDS] [--dry-run]
Without --branch, candidates come from
    gh pr list --state open --draft=false --base main
ordered oldest first (FIFO by PR number).

Exit codes: 0 nothing errored (rejected/conflict branches are a normal
result, not a failure); 1 only under --dry-run, when some branch would not
have landed; 2 a branch errored (bad gate binary, ghost branch, push
failure - a system fault, not a normal red).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

GATE_DEFAULT = ["bash", "tools/train/gate.sh"]
GATE_TIMEOUT_DEFAULT = 5400


@dataclasses.dataclass(frozen=True, eq=False)
class TrainResult:
    branch: str
    status: str  # landed | rejected | conflict | error
    detail: str = ""

    def __eq__(self, other):  # detail is informational, not identity
        return (
            isinstance(other, TrainResult)
            and self.branch == other.branch
            and self.status == other.status
        )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=600
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def _state_dir(repo: Path) -> Path:
    state = repo / ".train"
    state.mkdir(exist_ok=True)
    return state


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_lock(repo: Path) -> Path:
    lock = _state_dir(repo) / "lock"
    try:
        lock.mkdir()
    except FileExistsError:
        pid_file = lock / "pid"
        try:
            holder = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            holder = None
        if holder is not None and _pid_alive(holder):
            raise SystemExit(
                f"another train run holds .train/lock (held by pid {holder}; "
                "remove .train/lock if certain no train runs)"
            ) from None
        print(f"reclaimed stale train lock (pid {holder} dead)")
        shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    return lock


def _cache_key(tree: str, gate: list[str]) -> str:
    """Cache identity = merged tree AND the exact gate that approved it.

    A bare tree hash would let a cheap or dry-run gate pre-approve the tree
    for a later run with the real gate - proven exploitable in review. Gate
    argv is NUL-joined, not space-joined: ["a b", "c"] and ["a", "b c"] are
    different commands and must not collide onto the same cache key.
    """
    return hashlib.sha256((tree + "\0" + "\0".join(gate)).encode()).hexdigest()


def _validated_keys(repo: Path) -> set[str]:
    path = _state_dir(repo) / "validated_trees.txt"
    if not path.exists():
        return set()
    try:
        return set(path.read_text(encoding="utf-8").split())
    except (OSError, UnicodeDecodeError) as exc:
        # A corrupt cache degrades to "re-run gates", never to a crash - and
        # it must not keep failing every read forever. Quarantine the file
        # (best-effort) so the next successful land starts a clean one.
        quarantine = path.with_suffix(".corrupt")
        try:
            os.replace(path, quarantine)
            print(f"train: quarantined unreadable gate cache to {quarantine.name} ({exc})")
        except OSError as replace_exc:
            print(
                f"train: ignoring unreadable gate cache ({exc}); quarantine failed: {replace_exc}"
            )
        return set()


def _record_key(repo: Path, key: str) -> None:
    with open(_state_dir(repo) / "validated_trees.txt", "a", encoding="utf-8") as f:
        f.write(key + "\n")


def candidates_from_gh(repo: Path) -> list[str]:
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--draft=false",
            "--base",
            "main",
            "--limit",
            "100",
            "--json",
            "number,headRefName",
        ],
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh pr list failed: {proc.stderr.strip()}")
    prs = sorted(json.loads(proc.stdout), key=lambda p: p["number"])
    return [p["headRefName"] for p in prs]


def process_branch(
    repo: Path, branch: str, gate: list[str], dry_run: bool, gate_timeout: int
) -> TrainResult:
    _git(repo, "fetch", "origin", "--prune")
    _git(repo, "checkout", "main")
    _git(repo, "reset", "--hard", "origin/main")

    replay = _git(repo, "merge-tree", "--write-tree", f"origin/{branch}", "main", check=False)
    ghost_signatures = ("not something we can merge", "could not resolve")
    if replay.returncode != 0 and any(
        sig in (replay.stderr + replay.stdout) for sig in ghost_signatures
    ):
        return TrainResult(branch, "error", f"branch origin/{branch} not found on origin")
    if replay.returncode == 1:
        files = ", ".join(replay.stdout.splitlines()[1:6])
        return TrainResult(branch, "conflict", f"textual conflict vs main: {files}")
    if replay.returncode > 1:
        return TrainResult(branch, "error", replay.stderr.strip())

    merge = _git(
        repo,
        "merge",
        "--no-ff",
        f"origin/{branch}",
        "-m",
        f"train: merge {branch}",
        check=False,
    )
    if merge.returncode != 0:
        _git(repo, "merge", "--abort", check=False)
        _git(repo, "reset", "--hard", "origin/main")
        return TrainResult(branch, "conflict", merge.stderr.strip()[:400])

    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

    # Dry-run neither reads nor writes the cache: a dry-run must never
    # pre-approve a later real run, and must itself always exercise the gate.
    key = _cache_key(tree, gate)
    if dry_run or key not in _validated_keys(repo):
        # Popen + start_new_session (not subprocess.run's timeout=) so a
        # timeout can kill the WHOLE process group. A gate that shells out
        # (bash -> pytest) leaves the real work as a grandchild: killing only
        # the direct child on timeout lets it reparent to PID 1 and keep
        # burning the host while the train moves on.
        gate_proc = subprocess.Popen(
            gate,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            gate_out, gate_err = gate_proc.communicate(timeout=gate_timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(gate_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # already exited between the timeout and the kill
            gate_proc.communicate()  # reap, discard output
            _git(repo, "reset", "--hard", "origin/main")
            return TrainResult(
                branch,
                "rejected",
                f"gate timed out after {gate_timeout}s (process group killed)",
            )
        if gate_proc.returncode != 0:
            tail = (gate_out + gate_err)[-2000:]
            _git(repo, "reset", "--hard", "origin/main")
            return TrainResult(branch, "rejected", f"gate failed:\n{tail}")
        if not dry_run:
            _record_key(repo, key)

    if dry_run:
        _git(repo, "reset", "--hard", "origin/main")
        return TrainResult(branch, "landed", "dry-run: validated, not pushed")

    push = _git(repo, "push", "origin", "main", check=False)
    if push.returncode != 0:
        _git(repo, "reset", "--hard", "origin/main")
        return TrainResult(branch, "error", f"push failed: {push.stderr.strip()[:400]}")
    return TrainResult(branch, "landed")


def run_train(
    repo: Path,
    branches: list[str] | None = None,
    gate: list[str] | None = GATE_DEFAULT,
    gate_factory=None,
    dry_run: bool = False,
    gate_timeout: int = GATE_TIMEOUT_DEFAULT,
) -> list[TrainResult]:
    repo = Path(repo).resolve()
    lock = _acquire_lock(repo)
    results: list[TrainResult] = []
    try:
        queue = branches if branches is not None else candidates_from_gh(repo)
        for branch in queue:
            branch_gate = gate_factory(branch) if gate_factory else gate
            try:
                result = process_branch(repo, branch, branch_gate, dry_run, gate_timeout)
            except Exception as exc:  # noqa: BLE001 - one broken branch must not stall the queue
                result = TrainResult(branch, "error", f"{type(exc).__name__}: {exc}")
                _git(repo, "reset", "--hard", "origin/main", check=False)
            results.append(result)
            print(
                f"train: {result.branch} -> {result.status}"
                + (f" ({result.detail.splitlines()[0][:120]})" if result.detail else "")
            )
    finally:
        shutil.rmtree(lock, ignore_errors=True)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("train summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--branch", action="append", default=None)
    run_parser.add_argument("--gate", type=str, default=None)
    run_parser.add_argument("--gate-timeout", type=int, default=GATE_TIMEOUT_DEFAULT)
    run_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    gate = shlex.split(args.gate) if args.gate else GATE_DEFAULT
    results = run_train(
        repo=args.repo,
        branches=args.branch,
        gate=gate,
        dry_run=args.dry_run,
        gate_timeout=args.gate_timeout,
    )
    # rejected/conflict are NORMAL train outcomes (the queue did its job);
    # only a system error is a failing exit for cron/loop wrappers. Under
    # --dry-run there is no push to distinguish green from red by, so a
    # non-landed branch also fails the exit code.
    if any(r.status == "error" for r in results):
        return 2
    if args.dry_run and any(r.status != "landed" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
