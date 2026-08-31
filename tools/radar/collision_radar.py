"""Collision radar: in-memory merge replay across all live branch pairs.

Simulating the merge is cheap and exact, unlike risk prediction (which the
literature shows does not work). Uses `git merge-tree --write-tree`, which
merges without touching any checkout (git >= 2.38).

Usage:
    python tools/radar/collision_radar.py [--repo PATH] [--json] [--fail-on-conflict]
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
from pathlib import Path

LIVE_PREFIXES = ("claude/", "fix/", "feat/")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )


def live_branches(repo: Path) -> list[str]:
    """Branches with commits not on main, filtered to work-track prefixes."""
    out = _git(repo, "for-each-ref", "refs/heads", "--format=%(refname:short)")
    branches = []
    for name in out.stdout.split():
        if not name.startswith(LIVE_PREFIXES):
            continue
        count = _git(repo, "rev-list", "--count", f"main..{name}")
        if count.returncode == 0 and int(count.stdout.strip() or 0) > 0:
            branches.append(name)
    return sorted(branches)


def replay_pair(repo: Path, a: str, b: str) -> dict:
    """Merge a and b in memory; report cleanliness and conflicted files."""
    result = _git(repo, "merge-tree", "--write-tree", "--name-only", a, b)
    if result.returncode == 0:
        return {"a": a, "b": b, "clean": True, "files": []}
    if result.returncode == 1:
        # Output shape: <written tree OID>\n<conflicted file names>\n\n<informational
        # messages (Auto-merging ..., CONFLICT ...)>. Keep only the first paragraph
        # (OID + names) so diagnostic prose from the second paragraph never leaks
        # into "files".
        body = result.stdout.split("\n\n", 1)[0]
        lines = [ln for ln in body.splitlines() if ln.strip()]
        return {"a": a, "b": b, "clean": False, "files": lines[1:]}
    raise RuntimeError(f"merge-tree failed for {a} x {b}: {result.stderr.strip()}")


def scan(repo: Path) -> list[dict]:
    branches = live_branches(repo)
    results = [replay_pair(repo, branch, "main") for branch in branches]
    results += [replay_pair(repo, a, b) for a, b in itertools.combinations(branches, 2)]
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-conflict", action="store_true")
    args = parser.parse_args()

    results = scan(args.repo)
    conflicts = [r for r in results if not r["clean"]]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"RADAR: {len(results)} pairs replayed, {len(conflicts)} on collision course.")
        for r in conflicts:
            print(f"  {r['a']} x {r['b']}: {', '.join(r['files'])}")
    return 1 if (conflicts and args.fail_on_conflict) else 0


if __name__ == "__main__":
    sys.exit(main())
