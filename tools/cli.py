"""switchyard: the unified CLI over this toolkit's separate modules.

Subcommands:
    switchyard status [--repo PATH]
        A `notify: <mode>` line (SwitchyardConfig.notify, see
        tools/lib/notify.py) followed by one composed, read-only terminal
        view: WIP (live tracks vs the configured cap), RADAR (conflict
        pairs), QUEUE (open PRs via gh, ready vs draft, priority-labeled
        marked), FLAKY (.train/flaky_log.jsonl - process_branch's retry-
        rescued gates, see merge_train.py's _append_flaky_log; this only
        ever reads it), LAST LANDINGS (the tail of .train/history.jsonl,
        humanized). Every section is independently defensive: a missing
        file, an absent `gh`, or an unreadable repo degrades that ONE
        section to a one-line explanation and never takes the rest of the
        view down with it.

    switchyard stats [--repo PATH] [--days N]
        Aggregates .train/history.jsonl (see tools/train/merge_train.py's
        _append_history) over the last N days (default 14): counts per
        status, landing rate/day, mean + p90 gate_seconds, and the most
        frequently rejected branches.

    switchyard radar [...]
    switchyard land [...]
        Thin passthroughs: every flag is forwarded verbatim to
        tools/radar/collision_radar.py's / tools/train/merge_train.py's own
        argument parsers (`land` implies merge_train's `run` subcommand) -
        this file never reimplements their argument handling, so behavior
        can never drift between calling `switchyard land ...` and calling
        `python tools/train/merge_train.py run ...` directly.

    switchyard track new <name> [--repo PATH]
    switchyard track done <name> [--repo PATH] [--force-local] [--dry-run]
        Track lifecycle - see cmd_track_new/cmd_track_done below.

    switchyard watch install [--repo PATH] [--interval SECONDS] [--dry-run]
    switchyard watch uninstall [--repo PATH] [--dry-run]
    switchyard watch status [--repo PATH]
        Opt-in launchd agent (macOS only) that runs `bin/switchyard land
        --repo <station> --land pr-squash --batch <cfg.batch>` every
        `--interval` seconds (default 1200). NOT enabled anywhere by
        default - `install` refuses with a clear message if
        switchyard.toml's `station` is unset, and every subcommand takes
        `--dry-run` to print what it would do (the plist XML for `install`,
        the unload+remove actions for `uninstall`) without touching
        launchctl or the filesystem. See cmd_watch_install below.
"""

from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "train"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "radar"))
import collision_radar  # noqa: E402
import merge_train  # noqa: E402
from switchyard_config import load_config  # noqa: E402

DEFAULT_STATS_DAYS = 14


def _humanize_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h"
    days = hours / 24
    return f"{int(days)}d"


def _read_jsonl(path: Path) -> list[dict] | None:
    """Read a JSONL file into a list of dicts, skipping unparseable lines.

    Returns None if `path` does not exist or cannot be read at all (caller
    renders the "not present"/"could not read" message); an empty list means
    the file exists but has no usable lines.
    """
    if not path.is_file():
        return None
    try:
        raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    entries = []
    for line in raw_lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# --- switchyard status --------------------------------------------------------


def _print_wip_section(repo: Path, cfg) -> None:
    try:
        branches = collision_radar.live_branches(
            repo, tuple(cfg.live_prefixes), cfg.protected_branch
        )
    except Exception as exc:  # noqa: BLE001 - one section must never sink the whole view
        print(f"  could not compute WIP: {exc}")
        return
    marker = " - CAP EXCEEDED" if len(branches) > cfg.wip_cap else ""
    print(f"  {len(branches)}/{cfg.wip_cap} live tracks{marker}")
    for branch in branches:
        print(f"    {branch}")


def _print_radar_section(repo: Path, cfg) -> None:
    try:
        results = collision_radar.scan(repo, tuple(cfg.live_prefixes), cfg.protected_branch)
    except Exception as exc:  # noqa: BLE001 - one section must never sink the whole view
        print(f"  could not run radar: {exc}")
        return
    conflicts = [r for r in results if not r["clean"]]
    print(f"  {len(results)} pairs replayed, {len(conflicts)} on collision course")
    for r in conflicts:
        print(f"    {r['a']} x {r['b']}: {', '.join(r['files'])}")


def _print_queue_section(repo: Path, cfg) -> None:
    gh = merge_train._gh_exe()
    try:
        proc = subprocess.run(
            [
                gh,
                "pr",
                "list",
                "--state",
                "open",
                "--base",
                cfg.protected_branch,
                "--limit",
                "100",
                "--json",
                "number,headRefName,isDraft,labels",
            ],
            capture_output=True,
            text=True,
            cwd=repo,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("  gh unavailable")
        return
    if proc.returncode != 0:
        print("  gh unavailable")
        return
    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("  gh unavailable (unparseable output)")
        return
    if not prs:
        print("  no open PRs")
        return
    for pr in sorted(prs, key=lambda p: merge_train._pr_sort_key(p, cfg.priority_label)):
        state = "draft" if pr.get("isDraft") else "ready"
        labels = {lb.get("name") for lb in (pr.get("labels") or [])}
        flag = " [priority]" if cfg.priority_label in labels else ""
        print(f"    #{pr['number']} {pr['headRefName']} ({state}){flag}")


def _print_flaky_section(repo: Path) -> None:
    # merge_train.py's process_branch writes one line here per gate that
    # failed then passed on an identical immediate retry (see
    # _append_flaky_log) - this only ever reads it, defensively, and renders
    # whatever is there.
    entries = _read_jsonl(repo / ".train" / "flaky_log.jsonl")
    if entries is None:
        print("  no flaky log yet (.train/flaky_log.jsonl not present)")
        return
    if not entries:
        print("  flaky log is empty")
        return
    now = time.time()
    for entry in entries[-10:]:
        label = entry.get("branch") or entry.get("test") or entry.get("name")
        if label is None:
            label = json.dumps(entry)[:60]
        ts = entry.get("ts")
        age = _humanize_age(now - ts) if isinstance(ts, (int, float)) else "unknown age"
        print(f"    {label} ({age} ago)")


def _print_last_landings_section(repo: Path) -> None:
    entries = _read_jsonl(repo / ".train" / "history.jsonl")
    if entries is None:
        print("  no landings recorded yet (.train/history.jsonl not present)")
        return
    if not entries:
        print("  history log is empty")
        return
    now = time.time()
    for entry in entries[-5:]:
        ts = entry.get("ts", now)
        age = _humanize_age(now - ts) if isinstance(ts, (int, float)) else "unknown age"
        gate_s = entry.get("gate_seconds", 0.0) or 0.0
        print(
            f"    {entry.get('branch', '?')} -> {entry.get('status', '?')} "
            f"({age} ago, gate {gate_s:.1f}s)"
        )


def cmd_status(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    cfg = load_config(repo)

    print(f"notify: {cfg.notify}")
    print()
    print("== WIP ==")
    _print_wip_section(repo, cfg)
    print()
    print("== RADAR ==")
    _print_radar_section(repo, cfg)
    print()
    print("== QUEUE ==")
    _print_queue_section(repo, cfg)
    print()
    print("== FLAKY ==")
    _print_flaky_section(repo)
    print()
    print("== LAST LANDINGS ==")
    _print_last_landings_section(repo)
    return 0


# --- switchyard stats ----------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default 'linear' method)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def cmd_stats(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    days = args.days
    print(f"switchyard stats - last {days} day(s)")

    entries = _read_jsonl(repo / ".train" / "history.jsonl")
    if entries is None:
        print("  no history recorded yet (.train/history.jsonl not present)")
        return 0

    cutoff = time.time() - days * 86400
    entries = [e for e in entries if e.get("ts", 0) >= cutoff]

    print(f"  {len(entries)} landing attempt(s) in window")
    if not entries:
        return 0

    print()
    print("by status:")
    counts = Counter(e.get("status", "?") for e in entries)
    for status, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {status}: {n}")

    landed = counts.get("landed", 0)
    print()
    print(f"landing rate: {landed / days:.2f}/day ({landed} landed over {days} day(s))")

    gate_times = sorted(
        e["gate_seconds"] for e in entries if e.get("gate_seconds") and e["gate_seconds"] > 0
    )
    if gate_times:
        mean_s = statistics.mean(gate_times)
        p90_s = _percentile(gate_times, 90)
        print()
        print(f"gate time: mean {mean_s:.1f}s, p90 {p90_s:.1f}s (n={len(gate_times)})")

    rejected = Counter(e["branch"] for e in entries if e.get("status") == "rejected")
    if rejected:
        print()
        print("top rejected branches:")
        for branch, n in rejected.most_common(5):
            print(f"  {branch}: {n}")

    return 0


# --- switchyard radar / land: thin passthroughs -------------------------------
#
# Deliberately NOT routed through argparse subparsers: argparse.REMAINDER is
# well known to mishandle a positional that starts with its own leading
# `-`/`--` token right after the subcommand (e.g. `switchyard radar --repo
# X` would misparse `--repo` as an argument of the TOP-LEVEL parser and
# reject it as unrecognized). Intercepting the raw argv before argparse ever
# sees it sidesteps that entirely, and as a side benefit makes `switchyard
# radar --help` show collision_radar.py's own --help verbatim, which is the
# actually-correct behavior for a thin passthrough.

PASSTHROUGH_COMMANDS = ("radar", "land")


def cmd_radar(rest: list[str]) -> int:
    old_argv = sys.argv
    try:
        sys.argv = ["collision_radar.py", *rest]
        return collision_radar.main()
    finally:
        sys.argv = old_argv


def cmd_land(rest: list[str]) -> int:
    old_argv = sys.argv
    try:
        sys.argv = ["merge_train.py", "run", *rest]
        return merge_train.main()
    finally:
        sys.argv = old_argv


# --- switchyard track new / done: track lifecycle -----------------------------


def _find_worktree_for_branch(repo: Path, branch: str) -> Path | None:
    """The real, currently-registered worktree path for `branch`, via git itself.

    Trusting the configured worktree_dir/name alone would drift the moment
    a worktree is moved or was created some other way; `git worktree list`
    is the one source of truth for where a branch's worktree actually lives.
    Returns None if `branch` has no worktree registered at all.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    current_path: str | None = None
    target_ref = f"refs/heads/{branch}"
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.startswith("branch ") and current_path and line[len("branch ") :] == target_ref:
            return Path(current_path)
    return None


def cmd_track_new(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    cfg = load_config(repo)
    name = args.name

    if not cfg.worktree_dir:
        print(
            "switchyard track new: worktree_dir is not set in switchyard.toml - "
            "set [switchyard].worktree_dir to the directory track worktrees should live in"
        )
        return 2

    branch = f"{cfg.branch_prefix}{name}"
    worktree_path = Path(cfg.worktree_dir).expanduser() / name
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic: creates `branch` from the protected branch's tip AND attaches
    # the worktree to it in one step, so there is no intermediate state
    # where the branch exists but no worktree does (or vice versa).
    add = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            cfg.protected_branch,
        ],
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        print(f"switchyard track new: could not create branch/worktree: {add.stderr.strip()}")
        return 2

    push = subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", branch],
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        print(
            f"switchyard track new: push failed (branch + worktree still created locally): "
            f"{push.stderr.strip()}"
        )

    gh = merge_train._gh_exe()
    pr_title = f"wip: {name}"
    pr_body = (
        f"Work track for `{branch}`.\n\n"
        "<!-- write-zone: fill in what this track owns/touches, and what it must not touch. -->\n"
    )
    gh_create = [
        gh,
        "pr",
        "create",
        "--title",
        pr_title,
        "--draft",
        "--body",
        pr_body,
        "--head",
        branch,
        "--base",
        cfg.protected_branch,
    ]
    try:
        pr = subprocess.run(gh_create, capture_output=True, text=True, cwd=repo, timeout=60)
        gh_ok = pr.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        gh_ok = False

    print(f"worktree ready: {worktree_path}")
    print(f"branch: {branch}")
    if gh_ok:
        print("draft PR opened.")
    else:
        print("gh unavailable or PR creation failed - branch and worktree are ready anyway.")
        print("open the draft PR later with:")
        print(f"  {shlex.join(gh_create)}")
    print(f"next: cd {worktree_path} && start working")
    return 0


def cmd_track_done(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    cfg = load_config(repo)
    name = args.name
    branch = f"{cfg.branch_prefix}{name}"

    if not args.force_local:
        gh = merge_train._gh_exe()
        try:
            pr_list = subprocess.run(
                [
                    gh,
                    "pr",
                    "list",
                    "--head",
                    branch,
                    "--base",
                    cfg.protected_branch,
                    "--state",
                    "merged",
                    "--json",
                    "number",
                    "--limit",
                    "1",
                    "-q",
                    ".[0].number",
                ],
                capture_output=True,
                text=True,
                cwd=repo,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(
                f"switchyard track done: could not verify a MERGED PR via gh ({exc}) - "
                "pass --force-local to skip this check if you are certain"
            )
            return 2
        merged_number = pr_list.stdout.strip()
        if pr_list.returncode != 0 or not merged_number or merged_number == "null":
            print(
                f"switchyard track done: no MERGED PR found for {branch} - refusing to clean up "
                "(pass --force-local to skip this check if you are certain)"
            )
            return 2

    worktree_path = _find_worktree_for_branch(repo, branch)
    if worktree_path is None and cfg.worktree_dir:
        worktree_path = Path(cfg.worktree_dir).expanduser() / name

    if args.dry_run:
        print(f"[dry-run] would remove worktree: {worktree_path}")
        print(f"[dry-run] would delete local branch: {branch} (-D)")
        print(f"[dry-run] would delete remote branch: origin/{branch}")
        return 0

    if worktree_path is not None and Path(worktree_path).exists():
        # No --force: git itself refuses when the worktree has local
        # modifications or untracked files, which IS the "refuse if dirty"
        # check - no need to duplicate that logic here.
        remove = subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", str(worktree_path)],
            capture_output=True,
            text=True,
        )
        if remove.returncode != 0:
            print(f"switchyard track done: worktree remove refused: {remove.stderr.strip()}")
            return 2

    # -D, not -d: this repo's convention is squash-merge onto the protected
    # branch, so the track branch's own commits never become reachable from
    # main's ancestry - `git branch -d`'s "is this merged" check would
    # refuse every single track branch by design, even ones that landed
    # cleanly, so -D is the correct (not just the forceful) choice here.
    delete_local = subprocess.run(
        ["git", "-C", str(repo), "branch", "-D", branch],
        capture_output=True,
        text=True,
    )
    if delete_local.returncode != 0:
        print(
            f"switchyard track done: could not delete local branch: {delete_local.stderr.strip()}"
        )
        return 2

    delete_remote = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "--delete", branch],
        capture_output=True,
        text=True,
    )
    if delete_remote.returncode != 0:
        print(
            f"switchyard track done: remote branch delete skipped/failed (tolerated): "
            f"{delete_remote.stderr.strip()[:200]}"
        )

    print(f"track {name} cleaned up.")
    return 0


# --- switchyard watch install / uninstall / status: opt-in launchd watcher ----
#
# NOT enabled anywhere by default - install must be run explicitly, and even
# then only writes/loads a per-repo launchd agent under the current user's
# own ~/Library/LaunchAgents, never anything system-wide or root-owned.


def _watch_label(reponame: str) -> str:
    return f"com.switchyard.{reponame}"


def _watch_plist_path(reponame: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_watch_label(reponame)}.plist"


def _watch_plist_dict(
    reponame: str,
    switchyard_bin: Path,
    station: Path,
    batch: int,
    interval: int,
    log_path: Path,
) -> dict:
    """The launchd job description for periodic `switchyard land`.

    pr-squash, not push: a launchd-driven watcher runs unattended, so it
    must land through the same server-side ruleset/required-checks path a
    human-reviewed PR would - never a bare push straight to the protected
    branch. `batch` mirrors whatever the station's own switchyard.toml
    configures, so the watcher's batching matches manual `land` runs.
    """
    return {
        "Label": _watch_label(reponame),
        "ProgramArguments": [
            str(switchyard_bin),
            "land",
            "--repo",
            str(station),
            "--land",
            "pr-squash",
            "--batch",
            str(batch),
        ],
        "StartInterval": interval,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "RunAtLoad": False,
    }


def _watch_plist_xml(data: dict) -> str:
    import plistlib

    return plistlib.dumps(data, fmt=plistlib.FMT_XML).decode("utf-8")


def cmd_watch_install(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    cfg = load_config(repo)
    if not cfg.station:
        print(
            "switchyard watch install: station is not set in switchyard.toml - "
            "set [switchyard].station to the train's station clone path first"
        )
        return 2

    reponame = repo.name
    station = Path(cfg.station).expanduser()
    # bin/switchyard next to THIS toolkit checkout, not the target repo -
    # launchd jobs get no shell PATH/cwd, so this must be an absolute path.
    switchyard_bin = Path(__file__).resolve().parent.parent / "bin" / "switchyard"
    plist_path = _watch_plist_path(reponame)
    log_path = station / ".train" / "watch.log"
    data = _watch_plist_dict(reponame, switchyard_bin, station, cfg.batch, args.interval, log_path)
    xml = _watch_plist_xml(data)

    if args.dry_run:
        print(f"[dry-run] would write {plist_path}:")
        print(xml)
        print(f"[dry-run] would run: launchctl load -w {plist_path}")
        return 0

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(xml, encoding="utf-8")
    print(f"wrote {plist_path}")

    load = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True
    )
    if load.returncode == 0:
        print(f"loaded via launchctl - runs `switchyard land` every {args.interval}s")
    else:
        print(f"launchctl load failed (plist is written anyway): {load.stderr.strip()}")
        print(f"load it by hand with: launchctl load -w {plist_path}")

    print(f"verify with: launchctl list | grep {_watch_label(reponame)}")
    print(f"logs at: {log_path}")
    print(f"uninstall with: switchyard watch uninstall --repo {repo}")
    return 0


def cmd_watch_uninstall(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    reponame = repo.name
    plist_path = _watch_plist_path(reponame)

    if args.dry_run:
        print(f"[dry-run] would run: launchctl unload -w {plist_path}")
        print(f"[dry-run] would remove: {plist_path}")
        return 0

    unload = subprocess.run(
        ["launchctl", "unload", "-w", str(plist_path)], capture_output=True, text=True
    )
    if unload.returncode != 0:
        print(f"launchctl unload skipped/failed (tolerated): {unload.stderr.strip()}")

    if plist_path.exists():
        plist_path.unlink()
        print(f"removed {plist_path}")
    else:
        print(f"{plist_path} was not present - nothing to remove")
    return 0


def cmd_watch_status(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    reponame = repo.name
    plist_path = _watch_plist_path(reponame)

    if not plist_path.exists():
        print(f"not installed ({plist_path} does not exist)")
        return 0

    print(f"plist present: {plist_path}")
    proc = subprocess.run(
        ["launchctl", "list", _watch_label(reponame)], capture_output=True, text=True
    )
    if proc.returncode == 0:
        print("loaded in launchctl:")
        print(proc.stdout.strip())
    else:
        print("plist file exists but is not currently loaded in launchctl")
    return 0


# --- argument parsing ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="composed view: WIP, radar, queue, flaky, landings")
    p_status.add_argument("--repo", type=Path, default=Path.cwd())
    p_status.set_defaults(func=cmd_status)

    p_stats = sub.add_parser("stats", help="landing-history stats from .train/history.jsonl")
    p_stats.add_argument("--repo", type=Path, default=Path.cwd())
    p_stats.add_argument("--days", type=int, default=DEFAULT_STATS_DAYS)
    p_stats.set_defaults(func=cmd_stats)

    # radar/land are listed here only so `switchyard --help` shows them -
    # actual dispatch happens earlier in main(), before argparse ever
    # touches their arguments (see PASSTHROUGH_COMMANDS above).
    sub.add_parser("radar", help="passthrough to tools/radar/collision_radar.py (see --help there)")
    sub.add_parser(
        "land", help="passthrough to `tools/train/merge_train.py run` (see --help there)"
    )

    p_track = sub.add_parser("track", help="track lifecycle: new, done")
    track_sub = p_track.add_subparsers(dest="track_cmd", required=True)

    p_track_new = track_sub.add_parser(
        "new", help="create a branch + worktree + draft PR for a new track"
    )
    p_track_new.add_argument("name")
    p_track_new.add_argument("--repo", type=Path, default=Path.cwd())
    p_track_new.set_defaults(func=cmd_track_new)

    p_track_done = track_sub.add_parser(
        "done", help="verify the branch's PR merged, then remove its worktree + branches"
    )
    p_track_done.add_argument("name")
    p_track_done.add_argument("--repo", type=Path, default=Path.cwd())
    p_track_done.add_argument(
        "--force-local",
        action="store_true",
        help="skip the gh 'PR is MERGED' check (also used when gh is unavailable)",
    )
    p_track_done.add_argument(
        "--dry-run", action="store_true", help="print what would happen, change nothing"
    )
    p_track_done.set_defaults(func=cmd_track_done)

    p_watch = sub.add_parser(
        "watch", help="opt-in launchd periodic `switchyard land` (macOS only, off by default)"
    )
    watch_sub = p_watch.add_subparsers(dest="watch_cmd", required=True)

    p_watch_install = watch_sub.add_parser(
        "install", help="write + load a launchd agent that runs `switchyard land` periodically"
    )
    p_watch_install.add_argument("--repo", type=Path, default=Path.cwd())
    p_watch_install.add_argument(
        "--interval",
        type=int,
        default=1200,
        help="seconds between runs (launchd StartInterval, default 1200 = 20 minutes)",
    )
    p_watch_install.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plist that would be written instead of writing/loading it",
    )
    p_watch_install.set_defaults(func=cmd_watch_install)

    p_watch_uninstall = watch_sub.add_parser("uninstall", help="unload + remove the launchd agent")
    p_watch_uninstall.add_argument("--repo", type=Path, default=Path.cwd())
    p_watch_uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be unloaded/removed instead of doing it",
    )
    p_watch_uninstall.set_defaults(func=cmd_watch_uninstall)

    p_watch_status = watch_sub.add_parser(
        "status", help="report whether the launchd agent is installed and loaded"
    )
    p_watch_status.add_argument("--repo", type=Path, default=Path.cwd())
    p_watch_status.set_defaults(func=cmd_watch_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in PASSTHROUGH_COMMANDS:
        command, rest = argv[0], argv[1:]
        return cmd_radar(rest) if command == "radar" else cmd_land(rest)

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
