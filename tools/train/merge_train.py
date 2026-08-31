"""The merge train: the only writer to main.

One branch at a time: refresh main, replay-check, build the real merge, test
the COMBINED tree, land only on green. A red branch is reported and skipped -
it never blocks the branches behind it, and it never moves main.

The train runs as a normal user process (never through a Claude session's
Bash tool), so the session guard hooks do not apply to it - by design: the
guards ban sessions from pushing main precisely because the train does it.

Landing modes (--land):
    push (default) - `git push origin main` directly once the combined tree
        is gate-green. Correct when main has no server-side rule rejecting a
        plain push (e.g. this repo's own main).
    pr-squash - for a main that carries a GitHub ruleset (required status
        checks, non-fast-forward) which rejects a direct push with GH013.
        The local merge commit is still built and gate-tested exactly as in
        push mode - a red branch is caught identically - but landing goes
        through `gh pr merge <number> --squash` against the branch's open PR
        instead of pushing. process_branch captures the exact
        `origin/<branch>` commit SHA right before building that local merge
        and passes it as `--match-head-commit <sha>`, so GitHub itself
        refuses the squash server-side if the branch's head moved after the
        gate approved this SHA - an approved-then-moved race a bare
        `gh pr merge --squash` would land anyway. The branch's open PR is
        found via `gh pr list --head <branch> --base main --state open`; no
        open PR is a "no open PR for branch" error. After a successful gh
        merge, main is re-fetched and `origin/main^{tree}` is checked against
        the tree the gate just validated: squashing the same branch onto an
        unmoved main must reproduce that exact tree, so a mismatch means main
        moved (or something else is wrong) between the local test and the
        landing. That is reported loudly as an error ("... tree mismatch -
        INVESTIGATE") rather than silently trusted or rolled back. A gh merge
        failure is a normal rejected outcome for the queue, not a system
        error - main is reset locally to origin/main and the branch is
        reported rejected. When gh's stderr indicates the failure was the
        head-mismatch refusal, the detail is the specific "head moved after
        gating - re-queue" rather than the raw stderr, so a re-queue is
        obviously the right response; any other merge failure (for example
        required checks unmet) keeps gh's stderr tail as the detail. The `gh`
        executable name is read from the SWITCHYARD_GH env var (default
        "gh") so tests can inject a stub.

State (at the repo root):
    .train/validated_trees.txt  - cache keys (tree hash + gate argv, hashed)
                                  whose gate already passed
    .train/lock/                - mkdir-mutex (with pid file) so only one
                                  train runs; stale locks are reclaimed

Usage:
    python tools/train/merge_train.py run [--repo PATH] [--branch NAME ...]
        [--gate CMD] [--gate-timeout SECONDS] [--land {push,pr-squash}]
        [--dry-run]
Without --branch, candidates come from
    gh pr list --state open --draft=false --base main
ordered oldest first (FIFO by PR number).

Exit codes: 0 nothing errored (rejected/conflict branches are a normal
result, not a failure); 1 only under --dry-run, when some branch would not
have landed; 2 a branch errored (bad gate binary, ghost branch, landing
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


def _gh_exe() -> str:
    """The `gh` executable name/path - overridable so tests can inject a stub."""
    return os.environ.get("SWITCHYARD_GH", "gh")


def candidates_from_gh(repo: Path) -> list[str]:
    proc = subprocess.run(
        [
            _gh_exe(),
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


def _looks_like_head_mismatch(gh_output: str) -> bool:
    """Best-effort detection of a --match-head-commit rejection in gh's output.

    GitHub's merge endpoint rejects a stale expected-head SHA with wording
    that is not contractually stable across API versions ("Head branch was
    modified. Review and try the merge again." is the text at time of
    writing) - matched loosely (case-insensitive "head" plus a
    change-of-state word) so small upstream wording drift does not silently
    fall back to the generic "gh pr merge failed" detail. A false negative
    here still lands as a normal "rejected" outcome with the more generic
    detail text - never a wrongly-landed branch - so verify this heuristic
    against a real `gh` refusal before leaning on the specific detail text
    for automation.
    """
    lowered = gh_output.lower()
    return "head" in lowered and any(
        word in lowered for word in ("match", "modified", "moved", "changed")
    )


def _land_via_pr_squash(repo: Path, branch: str, validated_tree: str, head_sha: str) -> TrainResult:
    """Land the already gate-tested merge by squash-merging its PR through gh.

    Required when main carries a GitHub ruleset (required status checks,
    non-fast-forward) that rejects a direct `git push origin main` with
    GH013. process_branch's local merge commit exists only to compute and
    gate-test `validated_tree`; gh performs the actual landing server-side.
    `head_sha` is the `origin/<branch>` commit process_branch gate-tested,
    captured right before its local merge - passed to gh as
    `--match-head-commit` so a branch that moved after gating is refused by
    GitHub itself rather than squashed blind.
    """
    gh = _gh_exe()
    pr_list = subprocess.run(
        [
            gh,
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            "main",
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "1",
            "-q",
            ".[0].number",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if pr_list.returncode != 0:
        _git(repo, "reset", "--hard", "origin/main")
        return TrainResult(branch, "error", f"gh pr list failed: {pr_list.stderr.strip()[:400]}")

    # `.[0].number` on an empty PR list is jq null, not an error - a "null" or
    # blank stdout both mean the same thing here: no open PR to land through.
    pr_number = pr_list.stdout.strip()
    if not pr_number or pr_number == "null":
        _git(repo, "reset", "--hard", "origin/main")
        return TrainResult(branch, "error", f"no open PR for branch {branch}")

    merge = subprocess.run(
        [gh, "pr", "merge", pr_number, "--squash", "--match-head-commit", head_sha],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if merge.returncode != 0:
        _git(repo, "reset", "--hard", "origin/main")
        # Blocked by a ruleset (required checks unmet, etc.) is a normal red
        # for the queue, not a system fault - same status class as a failed gate.
        tail = (merge.stdout + merge.stderr).strip()[-2000:]
        if _looks_like_head_mismatch(tail):
            # The SHA gating approved is stale: GitHub itself caught the race
            # instead of us squashing a head we never actually tested.
            return TrainResult(branch, "rejected", "head moved after gating - re-queue")
        return TrainResult(branch, "rejected", f"gh pr merge failed:\n{tail}")

    _git(repo, "fetch", "origin")
    landed_tree = _git(repo, "rev-parse", "origin/main^{tree}").stdout.strip()
    if landed_tree != validated_tree:
        # main moved (or something else is wrong) between the local gate test
        # and gh's landing. Loud and stop - no rollback attempt, main is
        # already live and a guessed "fix" could make a real mess worse.
        return TrainResult(branch, "error", "landed but tree mismatch - INVESTIGATE")

    _git(repo, "reset", "--hard", "origin/main")
    return TrainResult(branch, "landed")


def process_branch(
    repo: Path,
    branch: str,
    gate: list[str],
    dry_run: bool,
    gate_timeout: int,
    land: str = "push",
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

    # Captured right before the merge so pr-squash mode can pin gh's landing
    # to exactly the commit gating approved (see _land_via_pr_squash).
    head_sha = _git(repo, "rev-parse", f"origin/{branch}").stdout.strip()

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

    if land == "pr-squash":
        return _land_via_pr_squash(repo, branch, tree, head_sha)

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
    land: str = "push",
) -> list[TrainResult]:
    repo = Path(repo).resolve()
    lock = _acquire_lock(repo)
    results: list[TrainResult] = []
    try:
        queue = branches if branches is not None else candidates_from_gh(repo)
        for branch in queue:
            branch_gate = gate_factory(branch) if gate_factory else gate
            try:
                result = process_branch(repo, branch, branch_gate, dry_run, gate_timeout, land)
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
    run_parser.add_argument("--land", choices=["push", "pr-squash"], default="push")
    run_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    gate = shlex.split(args.gate) if args.gate else GATE_DEFAULT
    results = run_train(
        repo=args.repo,
        branches=args.branch,
        gate=gate,
        dry_run=args.dry_run,
        gate_timeout=args.gate_timeout,
        land=args.land,
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
