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

Batch mode (--batch N, default 1 = today's plain per-branch behavior):
    Groups up to N queued branches into ONE candidate tree per gate run -
    bors-ng's documented strategy, O(failing x log N) gate runs instead of N.
    For a group: refresh main, then merge each member onto the evolving
    candidate in queue order (`git merge --no-ff origin/<branch>`,
    replay-checked first exactly like the single-branch path above). A member
    that conflicts against what has been built so far is set aside as
    "conflict" - it does not poison the rest of the group, which keeps
    building without it. The resulting candidate tree is gated ONCE (same
    cache key - tree + gate argv - and the same timeout/kill-process-group
    semantics as the single-branch path).
        Green: every member that made it into the candidate lands. Push mode
    pushes the candidate HEAD (all the sequential merge commits) in a single
    `git push`. pr-squash mode lands each member's PR with `gh pr merge
    --squash --match-head-commit <sha>` one at a time in queue order, then
    fetches and checks origin/main's tree against the gated candidate tree; a
    mismatch is a loud error ("batch landed but tree mismatch - INVESTIGATE")
    attached to the last member, never silently trusted. Honesty about the
    tradeoff: in pr-squash mode the INTERMEDIATE main states between member
    landings are never individually gated - only the fully-combined candidate
    was gate-tested before any of them landed; only the final state (after
    every member's squash) is verified equal to it. That's acceptable because
    landing here does not auto-deploy - deploys off this repo's main are a
    manual, separate step - but pass --batch 1 for a given run if even that
    window is unwanted.
        Red: the group is bisected in half (stable order) and each half gets
    a fresh candidate build + gate, recursively (depth bounded by log2 N). A
    half of size 1 that still gates red is a normal "rejected" - the same
    status a lone branch gets from the unbatched path.
        Every input branch, batched or not, ends with exactly one TrainResult.

Config (switchyard.toml, see tools/lib/switchyard_config.py): main() loads
the effective config and uses it to fill in whatever --gate/--gate-timeout/
--batch leave unset, and to supply the protected branch name and the
priority label candidates_from_gh sorts on. Nothing here reads config below
main() - run_train() and everything it calls take plain parameters, so
every existing caller (including every test in this file) that never
mentions config keeps today's exact hardcoded-default behavior.

Retry-once + flaky quarantine register (process_branch's single-branch
landing path only - never --batch, see _run_gate_with_retry's docstring):
when the gate fails for a reason OTHER than a timeout, the identical gate is
immediately rerun once before the branch is rejected. A rescue (fail then
pass) still lands the branch, but the failure is never silently thrown
away: one line is appended to .train/flaky_log.jsonl and a loud warning is
printed, so a human can look at the evidence and decide whether the test
that flaked deserves an actual quarantine - a decision this file
deliberately never makes on its own (see _run_gate_with_retry). Controlled
by SwitchyardConfig's retry_flaky (default True) / --no-retry-flaky.

State (at the repo root):
    .train/validated_trees.txt  - cache keys (tree hash + gate argv, hashed)
                                  whose gate already passed
    .train/lock/                - mkdir-mutex (with pid file) so only one
                                  train runs; stale locks are reclaimed
    .train/history.jsonl        - one JSON line per branch result (branch,
                                  status, detail_first_line, gate_seconds,
                                  tree, batch, ts) - see _append_history.
                                  Best-effort: a write failure never affects
                                  the TrainResult it was trying to log.
    .train/flaky_log.jsonl      - one JSON line per gate that failed then
                                  passed on process_branch's identical retry
                                  (tree, gate, first_tail, ts) - see
                                  _append_flaky_log. Same best-effort
                                  guarantee as history.jsonl.

Usage:
    python tools/train/merge_train.py run --repo PATH [--branch NAME ...]
        [--gate CMD] [--gate-timeout SECONDS] [--land {push,pr-squash}]
        [--batch N] [--dry-run] [--allow-dirty]
Without --branch, candidates come from
    gh pr list --state open --draft=false --base main
ordered with any priority-labeled PRs first, then oldest first (FIFO by PR
number) within each group.

--repo has no cwd default and is required: every branch `run`/`land`
processes gets `git reset --hard`, so a forgotten --repo defaulting to
whatever directory the command happened to run in - e.g. a live work
worktree - would silently destroy uncommitted work there. A repo that is
neither bare nor the exact path configured as [switchyard].station is also
refused up front if its working tree is dirty (`git status --porcelain`
nonempty), unless --allow-dirty is passed: those are the two shapes of repo
this file expects to freely reset, everything else is someone's real
checkout.

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
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from notify import notify as _send_notification
from switchyard_config import load_config

GATE_DEFAULT = ["bash", "tools/train/gate.sh"]
GATE_TIMEOUT_DEFAULT = 5400


def _notify_result(branch: str, result: TrainResult, notify_mode: str) -> None:
    """Fire a short, best-effort desktop notification for one finalized
    TrainResult - landed, rejected, error, or landed-via-flaky-retry (its
    own distinct message, since "fine" and "fine, but go look at
    flaky_log" are different signals worth telling apart at a glance).

    `conflict` is deliberately excluded: with several parallel work tracks
    it is an expected, frequent, not-actionable-mid-session outcome, and a
    notification per collision would be noise, not signal.

    Delegates to tools/lib/notify.py's notify(title, message, cfg), which
    wants a cfg-shaped object (a `.notify` attribute). run_train() takes
    `notify_mode` as a plain string, same as `protected`/`priority_label`/
    etc (see the module docstring's "Config" paragraph - nothing below
    main() touches a whole SwitchyardConfig), so it is wrapped here in a
    throwaway namespace just to satisfy notify()'s own signature.
    """
    if result.status == "landed" and result.flaky:
        title, message = "switchyard: flaky-landed", f"{branch} landed on retry - see flaky_log"
    elif result.status == "landed":
        title, message = "switchyard: landed", f"{branch} landed"
    elif result.status == "rejected":
        title, message = "switchyard: rejected", f"{branch} rejected"
    elif result.status == "error":
        title, message = "switchyard: error", f"{branch} errored - needs a look"
    else:
        return
    _send_notification(title, message, SimpleNamespace(notify=notify_mode))


@dataclasses.dataclass(frozen=True, eq=False)
class TrainResult:
    branch: str
    status: str  # landed | rejected | conflict | error
    detail: str = ""
    # Landing-history metadata (see _append_history) - informational, not
    # identity, same as `detail`: every existing TrainResult(branch, status)
    # / TrainResult(branch, status, detail) call site keeps working unchanged.
    gate_seconds: float = 0.0  # 0.0 when no gate ran at all (cache hit, or
    # rejected/error before a candidate tree ever existed)
    tree: str = ""  # candidate tree hash this result was decided against, "" if none
    batch_size: int = 1  # size of the candidate group this result was decided within
    flaky: bool = False  # True: this "landed" only after process_branch's identical
    # gate retry rescued it - see _run_gate_with_retry. Always False for anything
    # that isn't a landed, single-branch (non-batch) result.

    def __eq__(self, other):  # detail/history fields are informational, not identity
        return (
            isinstance(other, TrainResult)
            and self.branch == other.branch
            and self.status == other.status
        )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
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


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def _acquire_lock(repo: Path) -> Path:
    """Take the train's mutex: an O_EXCL-created .train/lock/pid file.

    The PID FILE is the actual mutex, not the containing directory. The
    previous scheme (mkdir the lock dir, THEN write a pid file inside it as
    a separate step) had a real TOCTOU window between those two steps: a
    second process could see the directory already exists, find no pid file
    written yet (or race a stale one), decide the lock was abandoned, and
    rmtree+recreate it out from under a process that was still
    mid-acquisition - proven exploitable in review (both trains then push
    the protected branch, and whichever `finally` runs first deletes the
    other's lock). `os.open(path, O_CREAT | O_EXCL | O_WRONLY)` creates the
    file and checks exclusivity in one atomic kernel call, closing that
    window entirely: only one process can ever win that create for a given
    path, full stop.

    On losing that race (FileExistsError): read whoever's pid is in there. A
    live holder is a normal, clean refusal (SystemExit - same message as
    before). A dead/unreadable holder means the previous run crashed without
    cleaning up - reclaim by unlinking the stale pid file and retrying the
    SAME atomic create exactly once. If that retry ALSO loses the race, some
    other process reclaimed it first in the meantime; rather than loop
    (which could spin forever under contention), that is simply treated as
    "held" and refused, same as an ordinary live holder.

    The directory itself (repo/.train/lock) is still created first (mkdir
    with exist_ok=True) purely as the pid file's container - it carries no
    locking semantics of its own anymore, so two processes racing on that
    mkdir is harmless (exist_ok=True lets both succeed).
    """
    lock = _state_dir(repo) / "lock"
    lock.mkdir(exist_ok=True)
    pid_file = lock / "pid"

    def try_create() -> bool:
        try:
            fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True

    if try_create():
        return lock

    holder = _read_pid(pid_file)
    if holder is not None and _pid_alive(holder):
        raise SystemExit(
            f"another train run holds .train/lock (held by pid {holder}; "
            "remove .train/lock if certain no train runs)"
        ) from None

    print(f"reclaimed stale train lock (pid {holder} dead)")
    try:
        os.unlink(pid_file)
    except FileNotFoundError:
        pass  # already gone - the retry below decides who actually wins it

    if try_create():
        return lock

    # Lost the reclaim race too: another process's create won between our
    # unlink and our retry. Whatever it wrote is the live lock now - refuse
    # rather than loop, exactly like an ordinary live-holder refusal.
    holder = _read_pid(pid_file)
    raise SystemExit(
        f"another train run holds .train/lock (held by pid {holder}; "
        "remove .train/lock if certain no train runs)"
    ) from None


def _release_lock(lock: Path) -> None:
    """Release the train mutex: remove the pid file (the actual lock), then
    tidy up the now-empty container directory.

    Order matters: unlinking the pid file first is what actually frees the
    mutex for the next acquirer. The directory removal after that is pure
    tidiness and must never raise - rmdir refuses a non-empty directory
    (e.g. a racing reclaimer recreated a pid file the instant after we
    removed ours) and that is fine, there is nothing to clean up in that
    case.
    """
    try:
        (lock / "pid").unlink()
    except FileNotFoundError:
        pass
    try:
        lock.rmdir()
    except OSError:
        pass


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


def _pr_sort_key(pr: dict, priority_label: str) -> tuple[int, int]:
    """Priority-labeled PRs land first; FIFO by PR number within each group -
    oldest first, same tie-break the queue always used before priority existed."""
    labels = {label.get("name") for label in (pr.get("labels") or [])}
    return (0 if priority_label in labels else 1, pr["number"])


def candidates_from_gh(
    repo: Path, protected: str = "main", priority_label: str = "train-priority"
) -> list[str]:
    proc = subprocess.run(
        [
            _gh_exe(),
            "pr",
            "list",
            "--state",
            "open",
            "--draft=false",
            "--base",
            protected,
            "--limit",
            "100",
            "--json",
            "number,headRefName,labels",
        ],
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh pr list failed: {proc.stderr.strip()}")
    prs = sorted(json.loads(proc.stdout), key=lambda p: _pr_sort_key(p, priority_label))
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


def _squash_one(
    repo: Path, gh: str, branch: str, head_sha: str, protected: str = "main"
) -> TrainResult | None:
    """Squash-merge `branch`'s open PR through gh, pinned to `head_sha`.

    Shared by the single-branch pr-squash landing path and the batch one -
    both need the exact same PR lookup, `gh pr merge --squash
    --match-head-commit` call, and failure classification, and must never be
    allowed to drift apart. Returns None on a successful squash (the caller
    still owns whatever comes after - the single-branch path's post-merge
    tree check, or the batch path's next member). Returns a TrainResult
    ("error" for no open PR or a `gh pr list` failure; "rejected" for a
    `gh pr merge` failure, with the specific "head moved after gating -
    re-queue" detail when the failure looks like a --match-head-commit
    rejection) when the squash itself did not happen - the caller resets the
    repo and reports it.
    """
    pr_list = subprocess.run(
        [
            gh,
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            protected,
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
        check=False,
    )
    if pr_list.returncode != 0:
        return TrainResult(branch, "error", f"gh pr list failed: {pr_list.stderr.strip()[:400]}")

    # `.[0].number` on an empty PR list is jq null, not an error - a "null" or
    # blank stdout both mean the same thing here: no open PR to land through.
    pr_number = pr_list.stdout.strip()
    if not pr_number or pr_number == "null":
        return TrainResult(branch, "error", f"no open PR for branch {branch}")

    merge = subprocess.run(
        [gh, "pr", "merge", pr_number, "--squash", "--match-head-commit", head_sha],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if merge.returncode != 0:
        # Blocked by a ruleset (required checks unmet, etc.) is a normal red
        # for the queue, not a system fault - same status class as a failed gate.
        tail = (merge.stdout + merge.stderr).strip()[-2000:]
        if _looks_like_head_mismatch(tail):
            # The SHA gating approved is stale: GitHub itself caught the race
            # instead of us squashing a head we never actually tested.
            return TrainResult(branch, "rejected", "head moved after gating - re-queue")
        return TrainResult(branch, "rejected", f"gh pr merge failed:\n{tail}")

    return None


def _land_via_pr_squash(
    repo: Path, branch: str, validated_tree: str, head_sha: str, protected: str = "main"
) -> TrainResult:
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
    failure = _squash_one(repo, gh, branch, head_sha, protected)
    if failure is not None:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return failure

    _git(repo, "fetch", "origin")
    landed_tree = _git(repo, "rev-parse", f"origin/{protected}^{{tree}}").stdout.strip()
    if landed_tree != validated_tree:
        # main moved (or something else is wrong) between the local gate test
        # and gh's landing. Loud and stop - no rollback attempt, main is
        # already live and a guessed "fix" could make a real mess worse.
        return TrainResult(branch, "error", "landed but tree mismatch - INVESTIGATE")

    _git(repo, "reset", "--hard", f"origin/{protected}")
    return TrainResult(branch, "landed")


def _run_gate(
    repo: Path, gate: list[str], gate_timeout: int, dry_run: bool, key: str
) -> tuple[str | None, float]:
    """Run `gate` in `repo`, honoring the validated-tree cache.

    Shared by the single-branch path and the batch path so the two can never
    drift apart on cache/timeout/kill-process-group semantics. Returns
    (detail, elapsed_seconds). `detail` is None on green (the gate passed, or
    the tree+gate combination was already cached) or a string describing why
    it is red (a timeout or the gate's own failure tail) - the caller decides
    what "red" means for it (reject one branch, or bisect a batch).
    `elapsed_seconds` is exactly 0.0 when the cache was hit (no process ever
    started) and the real wall-clock duration of the gate subprocess
    otherwise - including on timeout or failure - so the landing-history log
    (see _append_history) can tell a genuinely fast gate from a cache hit.

    Dry-run neither reads nor writes the cache: a dry-run must never
    pre-approve a later real run, and must itself always exercise the gate.
    """
    if not dry_run and key in _validated_keys(repo):
        return None, 0.0
    # Popen + start_new_session (not subprocess.run's timeout=) so a
    # timeout can kill the WHOLE process group. A gate that shells out
    # (bash -> pytest) leaves the real work as a grandchild: killing only
    # the direct child on timeout lets it reparent to PID 1 and keep
    # burning the host while the train moves on.
    start = time.perf_counter()
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
        elapsed = time.perf_counter() - start
        return f"gate timed out after {gate_timeout}s (process group killed)", elapsed
    elapsed = time.perf_counter() - start
    if gate_proc.returncode != 0:
        tail = (gate_out + gate_err)[-2000:]
        return f"gate failed:\n{tail}", elapsed
    if not dry_run:
        _record_key(repo, key)
    return None, elapsed


def _append_flaky_log(repo: Path, tree: str, gate: list[str], first_detail: str) -> None:
    """Append one line to .train/flaky_log.jsonl when a gate went red then
    green on process_branch's identical immediate retry.

    Best-effort, same guarantee as _append_history: a write failure here
    must never turn a real land into an error. `tree` is the candidate tree
    hash (not the sha256 cache key) so a human can correlate this file
    directly against history.jsonl's own "tree" field or validated_trees.txt
    - `switchyard status`'s FLAKY section (tools/cli.py) already reads
    exactly this file and needs no changes to start showing real entries.
    """
    entry = {
        "tree": tree,
        "gate": " ".join(gate),
        "first_tail": first_detail[-400:],
        "ts": time.time(),
    }
    try:
        state = _state_dir(repo)
        with open(state / "flaky_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"train: could not write flaky log ({exc})")


def _run_gate_with_retry(
    repo: Path,
    gate: list[str],
    gate_timeout: int,
    dry_run: bool,
    key: str,
    tree: str,
    retry_flaky: bool,
) -> tuple[str | None, float, bool]:
    """process_branch's gate execution: _run_gate, plus one identical retry
    on a genuine (non-timeout) failure before giving up.

    Chromium's commit queue retries a failing try job once against the exact
    same configuration before rejecting a CL - a real, reproducible failure
    fails the same way twice, so the retry costs one gate run and saves a
    good branch from a one-off flake. Mergify's blind auto-retry is the
    cautionary counter-example: retrying and landing silently on green
    LAUNDERS the signal - a real, reproducible bug can slip through
    disguised as "just flaky", with no trace left for anyone to notice.
    This keeps Chromium's forgiving behavior (a flaky gate does not block a
    good branch) while closing Mergify's hole: every retry-rescued land is
    appended to .train/flaky_log.jsonl (see _append_flaky_log) and printed
    as a loud warning, so the evidence survives even though the branch
    landed. Quarantining the actual flaky test remains a HUMAN decision made
    in the gate definition itself - this file never edits or skips a test on
    its own; the register plus `switchyard status`'s FLAKY section just hand
    a human the evidence, and the timestamp gives every entry an implicit
    expiry (an old, never-repeated one is not worth chasing).

    Only ever called from process_branch. _run_batch calls plain _run_gate
    directly and must keep doing so: a batch candidate's gate outcome is
    deterministic in the tree it tests, and test_batch_red_bisects_to_culprit
    asserts an exact gate-run count for its bisection - retrying there would
    silently double every red run's count and desync that accounting. Retry-
    once is deliberately scoped to the single-branch landing path only.

    A timeout is never retried: a hung gate will not run faster the second
    time, and retrying it only doubles the wait before a rejection the first
    run already decided.

    Returns (detail, total_elapsed_seconds, flaky) - `flaky` is True only
    when the first run failed (non-timeout) and the retry then passed.
    """
    detail, seconds = _run_gate(repo, gate, gate_timeout, dry_run, key)
    if detail is None or not retry_flaky or detail.startswith("gate timed out"):
        return detail, seconds, False

    first_detail = detail
    retry_detail, retry_seconds = _run_gate(repo, gate, gate_timeout, dry_run, key)
    total_seconds = seconds + retry_seconds

    if retry_detail is None:
        print("FLAKY GATE: landed on retry - signal preserved in flaky_log")
        _append_flaky_log(repo, tree, gate, first_detail)
        return None, total_seconds, True

    both_tails = (
        "gate failed twice (retried once, still red):\n"
        f"--- first run tail ---\n{first_detail[-400:]}\n"
        f"--- retry run tail ---\n{retry_detail[-400:]}"
    )
    return both_tails, total_seconds, False


def process_branch(
    repo: Path,
    branch: str,
    gate: list[str],
    dry_run: bool,
    gate_timeout: int,
    land: str = "push",
    protected: str = "main",
    retry_flaky: bool = False,
) -> TrainResult:
    _git(repo, "fetch", "origin", "--prune")
    _git(repo, "checkout", protected)
    _git(repo, "reset", "--hard", f"origin/{protected}")

    replay = _git(repo, "merge-tree", "--write-tree", f"origin/{branch}", protected, check=False)
    ghost_signatures = ("not something we can merge", "could not resolve")
    if replay.returncode != 0 and any(
        sig in (replay.stderr + replay.stdout) for sig in ghost_signatures
    ):
        return TrainResult(branch, "error", f"branch origin/{branch} not found on origin")
    if replay.returncode == 1:
        files = ", ".join(replay.stdout.splitlines()[1:6])
        return TrainResult(branch, "conflict", f"textual conflict vs {protected}: {files}")
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
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return TrainResult(branch, "conflict", merge.stderr.strip()[:400])

    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

    key = _cache_key(tree, gate)
    gate_detail, gate_seconds, gate_flaky = _run_gate_with_retry(
        repo, gate, gate_timeout, dry_run, key, tree, retry_flaky
    )
    if gate_detail is not None:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return TrainResult(branch, "rejected", gate_detail, gate_seconds, tree)

    if dry_run:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return TrainResult(
            branch,
            "landed",
            "dry-run: validated, not pushed",
            gate_seconds,
            tree,
            flaky=gate_flaky,
        )

    if land == "pr-squash":
        result = _land_via_pr_squash(repo, branch, tree, head_sha, protected)
        # gh can still fail the actual squash after a flaky-rescued gate pass
        # (no open PR, head moved, ...) - flaky must only ever describe a
        # landed result, never a rejected/error one riding gate_flaky along.
        landed_flaky = gate_flaky and result.status == "landed"
        return dataclasses.replace(result, gate_seconds=gate_seconds, tree=tree, flaky=landed_flaky)

    push = _git(repo, "push", "origin", protected, check=False)
    if push.returncode != 0:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return TrainResult(
            branch, "error", f"push failed: {push.stderr.strip()[:400]}", gate_seconds, tree
        )
    return TrainResult(branch, "landed", "", gate_seconds, tree, flaky=gate_flaky)


# --- Batch mode (--batch N) -------------------------------------------------
# See the module docstring's "Batch mode" section for the design in prose.


def _build_candidate_group(
    repo: Path, group: list[str], protected: str = "main"
) -> tuple[list[str], list[TrainResult], dict[str, str]]:
    """Sequentially merge `group`'s branches onto the checked-out main.

    Caller must already have refreshed main (fetch + checkout + reset --hard
    origin/main). This is the batch analogue of process_branch's replay
    check + `git merge --no-ff`, run once per member against the CANDIDATE
    built so far rather than against plain main. A member that fails its
    replay-check (ghost branch, textual conflict) or fails the real merge
    despite a clean replay-check is set aside with its final TrainResult and
    does not block the rest of the group - the candidate simply keeps
    growing without it.

    Returns (built, set_aside, head_shas):
        built - branch names actually merged into the candidate, in the
            order they were merged (a subsequence of `group`)
        set_aside - one TrainResult("conflict"|"error", ...) per member that
            did not make it into the candidate
        head_shas - origin/<branch> SHA captured right before each `built`
            member's own merge, keyed by branch - pr-squash landing needs
            this to pin `--match-head-commit` per member, same as the
            single-branch path.

    HEAD is left at the built candidate (or unchanged at main if `built`
    ends up empty).
    """
    built: list[str] = []
    set_aside: list[TrainResult] = []
    head_shas: dict[str, str] = {}
    for branch in group:
        replay = _git(
            repo, "merge-tree", "--write-tree", f"origin/{branch}", protected, check=False
        )
        ghost_signatures = ("not something we can merge", "could not resolve")
        if replay.returncode != 0 and any(
            sig in (replay.stderr + replay.stdout) for sig in ghost_signatures
        ):
            set_aside.append(
                TrainResult(branch, "error", f"branch origin/{branch} not found on origin")
            )
            continue
        if replay.returncode == 1:
            files = ", ".join(replay.stdout.splitlines()[1:6])
            set_aside.append(
                TrainResult(branch, "conflict", f"textual conflict vs candidate: {files}")
            )
            continue
        if replay.returncode > 1:
            set_aside.append(TrainResult(branch, "error", replay.stderr.strip()))
            continue

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
            # Abort just this failed attempt, not a full reset, so the rest
            # of the group keeps building on top of what came before it.
            _git(repo, "merge", "--abort", check=False)
            set_aside.append(TrainResult(branch, "conflict", merge.stderr.strip()[:400]))
            continue

        built.append(branch)
        head_shas[branch] = head_sha
    return built, set_aside, head_shas


def _land_batch_push(repo: Path, built: list[str], protected: str = "main") -> list[TrainResult]:
    """Push mode: the candidate HEAD already carries every `built` member's
    merge commit in sequence, so one `git push` lands all of them at once."""
    push = _git(repo, "push", "origin", protected, check=False)
    if push.returncode != 0:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        detail = f"push failed: {push.stderr.strip()[:400]}"
        return [TrainResult(b, "error", detail) for b in built]
    return [TrainResult(b, "landed") for b in built]


def _land_batch_pr_squash(
    repo: Path,
    built: list[str],
    validated_tree: str,
    head_shas: dict[str, str],
    protected: str = "main",
) -> list[TrainResult]:
    """pr-squash mode: land each member's PR one at a time, in queue order.

    Each squash is independently pinned to that member's own head_sha via
    _squash_one, same as the single-branch path. If a member's squash fails
    partway through, the members before it already landed for real (gh
    already squashed them server-side - no rollback, same "stop, don't
    guess" philosophy as the single-branch path's tree-mismatch case) and
    are reported "landed"; the failed member gets _squash_one's own
    error/rejected classification, and every member still unattempted after
    it is reported "rejected" so the train re-queues them next run rather
    than guessing whether landing them now, on top of a main that only
    partially matches what was gate-tested, would be safe.

    On a clean run through every member, origin/main is fetched once and its
    tree is checked against `validated_tree` - the ONE thing this whole
    group was actually gated against. A mismatch does not roll anything
    back (every member really did land); it is reported as a loud error
    attached to the last member. This is the design's explicit tradeoff:
    the intermediate main states between members were never individually
    gated, only this final combined state was - see the module docstring's
    "Batch mode" section.
    """
    gh = _gh_exe()
    results: list[TrainResult] = []
    for idx, branch in enumerate(built):
        failure = _squash_one(repo, gh, branch, head_shas[branch], protected)
        if failure is not None:
            _git(repo, "reset", "--hard", f"origin/{protected}")
            results.append(failure)
            results.extend(
                TrainResult(b, "rejected", f"batch landing halted: {branch} failed - re-queue")
                for b in built[idx + 1 :]
            )
            return results
        results.append(TrainResult(branch, "landed"))

    _git(repo, "fetch", "origin")
    landed_tree = _git(repo, "rev-parse", f"origin/{protected}^{{tree}}").stdout.strip()
    if landed_tree != validated_tree:
        results[-1] = TrainResult(
            built[-1], "error", "batch landed but tree mismatch - INVESTIGATE"
        )
    else:
        _git(repo, "reset", "--hard", f"origin/{protected}")
    return results


def _land_batch(
    repo: Path,
    built: list[str],
    tree: str,
    head_shas: dict[str, str],
    land: str,
    dry_run: bool,
    protected: str = "main",
) -> list[TrainResult]:
    if dry_run:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return [TrainResult(b, "landed", "dry-run: validated, not pushed") for b in built]
    if land == "pr-squash":
        return _land_batch_pr_squash(repo, built, tree, head_shas, protected)
    return _land_batch_push(repo, built, protected)


def _run_batch(
    repo: Path,
    group: list[str],
    gate: list[str],
    dry_run: bool,
    gate_timeout: int,
    land: str,
    protected: str = "main",
) -> list[TrainResult]:
    """Build a candidate from `group` (in order), gate it once, and on red
    bisect - bors-ng's O(failing x log N) strategy.

    Every call starts by refreshing main from scratch, so a bisected half is
    always a genuinely fresh candidate build + gate, per the design - never
    a reuse of anything left over from the larger group that just failed.
    Members that conflict or ghost during the build are set aside with
    their final TrainResult before the gate ever runs; they are simply
    stitched back into the result list here and never affect the
    gate/bisect decision, which is made purely on `built`.
    """
    _git(repo, "fetch", "origin", "--prune")
    _git(repo, "checkout", protected)
    _git(repo, "reset", "--hard", f"origin/{protected}")

    built, set_aside, head_shas = _build_candidate_group(repo, group, protected)
    group_size = len(group)
    set_aside = [dataclasses.replace(r, batch_size=group_size) for r in set_aside]
    if not built:
        return set_aside

    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    key = _cache_key(tree, gate)
    gate_detail, gate_seconds = _run_gate(repo, gate, gate_timeout, dry_run, key)

    if gate_detail is None:
        landed = _land_batch(repo, built, tree, head_shas, land, dry_run, protected)
        landed = [
            dataclasses.replace(r, gate_seconds=gate_seconds, tree=tree, batch_size=group_size)
            for r in landed
        ]
        return set_aside + landed

    # Red. A lone member is a normal rejection, not a further split - halving
    # a size-1 list forever would never terminate.
    if len(built) == 1:
        _git(repo, "reset", "--hard", f"origin/{protected}")
        return set_aside + [
            TrainResult(built[0], "rejected", gate_detail, gate_seconds, tree, group_size)
        ]

    mid = len(built) // 2
    left_results = _run_batch(repo, built[:mid], gate, dry_run, gate_timeout, land, protected)
    right_results = _run_batch(repo, built[mid:], gate, dry_run, gate_timeout, land, protected)
    return set_aside + left_results + right_results


def _log_result(result: TrainResult) -> None:
    print(
        f"train: {result.branch} -> {result.status}"
        + (f" ({result.detail.splitlines()[0][:120]})" if result.detail else "")
    )


def _append_history(repo: Path, result: TrainResult) -> None:
    """Best-effort append of one landing-history line to .train/history.jsonl.

    One JSON object per branch result, written right after run_train
    finalizes it: {branch, status, detail_first_line, gate_seconds, tree,
    batch, ts}. This is a durable record for `switchyard status`/`switchyard
    stats` to read later - it never feeds back into any landing decision, so
    a write failure (full disk, .train removed mid-run, a directory sitting
    where the file should be, ...) is swallowed with one warning, exactly
    like the gate-cache read/write path above: history-writing must never
    change or block a TrainResult.
    """
    entry = {
        "branch": result.branch,
        "status": result.status,
        "detail_first_line": result.detail.splitlines()[0] if result.detail else "",
        "gate_seconds": round(result.gate_seconds, 3),
        "tree": result.tree,
        "batch": result.batch_size,
        "ts": time.time(),
    }
    try:
        state = _state_dir(repo)
        with open(state / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"train: could not write landing history ({exc})")


def run_train(
    repo: Path,
    branches: list[str] | None = None,
    gate: list[str] | None = GATE_DEFAULT,
    gate_factory=None,
    dry_run: bool = False,
    gate_timeout: int = GATE_TIMEOUT_DEFAULT,
    land: str = "push",
    batch: int = 1,
    protected: str = "main",
    priority_label: str = "train-priority",
    retry_flaky: bool = False,
    notify_mode: str = "none",
) -> list[TrainResult]:
    repo = Path(repo).resolve()
    batch = max(1, batch)
    lock = _acquire_lock(repo)
    results: list[TrainResult] = []
    try:
        queue = (
            branches
            if branches is not None
            else candidates_from_gh(repo, protected, priority_label)
        )
        if batch <= 1:
            for branch in queue:
                branch_gate = gate_factory(branch) if gate_factory else gate
                try:
                    result = process_branch(
                        repo,
                        branch,
                        branch_gate,
                        dry_run,
                        gate_timeout,
                        land,
                        protected,
                        retry_flaky,
                    )
                except Exception as exc:  # noqa: BLE001 - one broken branch must not stall the queue
                    result = TrainResult(branch, "error", f"{type(exc).__name__}: {exc}")
                    _git(repo, "reset", "--hard", f"origin/{protected}", check=False)
                results.append(result)
                _log_result(result)
                _append_history(repo, result)
                _notify_result(branch, result, notify_mode)
        else:
            # gate_factory is inherently per-branch; a batch gates ONE
            # candidate tree for several branches at once, so there is no
            # single branch to key it on. Best-effort: the group's own gate
            # is gate_factory(first member) - documented, not exercised by
            # any test that combines gate_factory with batch>1.
            for i in range(0, len(queue), batch):
                group = queue[i : i + batch]
                group_gate = gate_factory(group[0]) if gate_factory else gate
                try:
                    group_results = _run_batch(
                        repo, group, group_gate, dry_run, gate_timeout, land, protected
                    )
                except Exception as exc:  # noqa: BLE001 - one broken group must not stall the queue
                    detail = f"{type(exc).__name__}: {exc}"
                    group_results = [
                        TrainResult(b, "error", detail, batch_size=len(group)) for b in group
                    ]
                    _git(repo, "reset", "--hard", f"origin/{protected}", check=False)
                # _run_batch's internal set-aside/bisect order does not track
                # queue order - re-sort into it here so the caller always
                # sees results in the same order the branches were queued,
                # exactly one TrainResult per member of `group`.
                by_branch = {r.branch: r for r in group_results}
                for branch in group:
                    result = by_branch[branch]
                    results.append(result)
                    _log_result(result)
                    _append_history(repo, result)
                    _notify_result(branch, result, notify_mode)
    finally:
        _release_lock(lock)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("train summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return results


def _repo_is_bare(repo: Path) -> bool:
    """True if `repo` is a bare git repository - no working tree, so it can
    never be "dirty" in the sense this file cares about."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _repo_is_dirty(repo: Path) -> bool:
    """True if `repo` has uncommitted changes to TRACKED files (staged or
    unstaged) - exactly what `git reset --hard` can destroy.

    Untracked files are deliberately excluded: `reset --hard` never touches
    them (only `git clean` does), so they are not the risk this check
    exists to catch - and this file's own .train/ bookkeeping directory
    (lock/, validated_trees.txt, history.jsonl, ...) is untracked debris in
    any repo that has no .gitignore entry for it, which would otherwise trip
    this check as "dirty" the moment a train run ever wrote to it, on the
    very first run.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _repo_matches_configured_station(repo: Path, cfg) -> bool:
    """True only if `repo` IS the exact clone [switchyard].station names.

    Not "any repo that happens to be configured somewhere" - the station is
    the one clone this whole toolkit is built to freely `git reset --hard`,
    so it is exempt from the dirty-working-tree refusal the same way a bare
    repo is. An unset station never matches anything.
    """
    if not cfg.station:
        return False
    try:
        return repo.resolve() == Path(cfg.station).expanduser().resolve()
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="the station clone to run the train against - required, never "
        "defaults to cwd: every branch processed gets `git reset --hard`, so "
        "an accidental cwd default inside a live work worktree would destroy "
        "uncommitted work there",
    )
    run_parser.add_argument("--branch", action="append", default=None)
    run_parser.add_argument("--gate", type=str, default=None)
    run_parser.add_argument("--gate-timeout", type=int, default=None)
    run_parser.add_argument("--land", choices=["push", "pr-squash"], default="push")
    run_parser.add_argument("--batch", type=int, default=None)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--no-retry-flaky",
        action="store_true",
        help="disable process_branch's identical-gate retry-once on failure "
        "(overrides switchyard.toml's retry_flaky for this run only)",
    )
    run_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow running against a non-bare, non-station repo whose working "
        "tree has uncommitted changes (run/land `git reset --hard` the "
        "checkout and would discard them) - not needed for a bare repo or "
        "the exact path configured as [switchyard].station",
    )
    args = parser.parse_args()

    cfg = load_config(args.repo)

    if (
        not args.allow_dirty
        and not _repo_is_bare(args.repo)
        and not _repo_matches_configured_station(args.repo, cfg)
        and _repo_is_dirty(args.repo)
    ):
        print(
            f"switchyard train: refusing to run against {args.repo} - its working "
            "tree has uncommitted changes and `run`/`land` will `git reset --hard` "
            "it, destroying them. Commit or stash first, point --repo at the "
            "actual station clone, or pass --allow-dirty if you are certain this "
            "is safe."
        )
        return 2

    if args.gate:
        gate = shlex.split(args.gate)
    elif cfg.gate_fast:
        gate = shlex.split(cfg.gate_fast)
    else:
        gate = GATE_DEFAULT
    gate_timeout = args.gate_timeout if args.gate_timeout is not None else cfg.gate_timeout
    batch = args.batch if args.batch is not None else cfg.batch
    retry_flaky = cfg.retry_flaky and not args.no_retry_flaky

    results = run_train(
        repo=args.repo,
        branches=args.branch,
        gate=gate,
        dry_run=args.dry_run,
        gate_timeout=gate_timeout,
        land=args.land,
        batch=batch,
        protected=cfg.protected_branch,
        priority_label=cfg.priority_label,
        retry_flaky=retry_flaky,
        notify_mode=cfg.notify,
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
