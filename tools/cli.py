"""switchyard: the unified CLI over this toolkit's separate modules.

Subcommands:
    switchyard status [--repo PATH]
        One composed, read-only terminal view: WIP (live tracks vs the
        configured cap), RADAR (conflict pairs), QUEUE (open PRs via gh,
        ready vs draft, priority-labeled marked), FLAKY (.train/flaky_log.jsonl,
        if present - a log another tool may start writing; this only ever
        reads it), LAST LANDINGS (the tail of .train/history.jsonl,
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
"""

from __future__ import annotations

import argparse
import json
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
    # Forward-compat: no tool in this repo writes .train/flaky_log.jsonl yet.
    # This only ever reads it, defensively, and renders whatever is there so
    # a future flaky-test tracker needs no changes here to show up.
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
