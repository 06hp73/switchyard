# Switchyard

Collision control for many parallel AI coding sessions working one git repo.
A switchyard is where trains from many tracks are sorted onto a single line —
this toolkit does the same for branches: many parallel work tracks, one
serialized, gate-tested path into `main`.

Built for (and battle-tested on) a solo-founder setup running 7+ Claude Code
sessions in git worktrees on one 8 GB machine. Everything is plain bash +
Python 3.12 stdlib. No dependencies beyond git ≥ 2.38, `jq`, and optionally
`pueue` and the GitHub CLI.

## The four collision classes it addresses

1. **Textual merge conflicts** between parallel branches.
2. **Semantic conflicts** — every branch green alone, wrong when combined.
3. **Runtime resource contention** — RAM, ports, Redis, one heavy solver.
4. **Sessions corrupting each other's git state** — the shared stash stack,
   identity drift, racing pushes.

The design follows the 2024–2026 research on parallel coding agents: agent
PRs conflict ~28 % of the time and the rate is driven by change size
(AgenticFlict, AIware 2026); enforced isolation beats polite instructions by
almost 8 points (CAID); a clean auto-merge is still logically wrong 5–10 % of
the time (CodeCRDT), so the only trustworthy gate is testing the *combined*
tree; and simulating merges is cheap and exact while risk prediction
demonstrably is not (Leßenich et al.).

## Components

| Path | What it does |
|---|---|
| `tools/guards/git_guard.sh` | PreToolUse hook: blocks `git stash` (repo-global across worktrees — silently destroys other sessions' work), identity changes (`git config user.*`, `-c`, `--author`, env overrides), force pushes (incl. `+refspec`), any push to `main`, and `rm -rf` on worktree dirs. Fail-open on parse errors. |
| `tools/guards/wip_status.sh` | Session-start banner: live (unmerged) track count vs the WIP cap. |
| `tools/guards/worktree_env.sh` | Deterministic per-worktree port + Redis-DB allocation. |
| `tools/guards/setup_pueue.sh` | pueue groups `solver`/`train` capped at 1 concurrent job machine-wide. |
| `tools/radar/collision_radar.py` | In-memory pairwise merge replay (`git merge-tree --write-tree`) across all live branches: a collision map hours before the collision. |
| `tools/train/merge_train.py` | The merge train — the only writer to `main`. One branch at a time: refresh, replay-check, real merge, run the gate on the combined tree, push only on green. Gate-identity cache (a dry-run or cheap gate can never pre-approve a real run), process-group kill on gate timeout, stale-lock reclaim, per-branch error containment. Red never moves `main` and never blocks the queue. |
| `tools/train/gate.sh`, `tools/train/gate_full.sh` | The gate definitions: `gate.sh` (fast tier, default) runs lint + fast suite + machine-local characterization goldens; `gate_full.sh` (full tier, select per-branch via `--gate` for branches touching optimizer math) additionally runs the analytic golden-master suite and the optimizer fuzz/superadditivity/certificate proofs the fast tier excludes. Both pin the tested tree to the station checkout via an import preflight. Adapt to your project. |
| `tools/train/json_merge_driver.py` | Key-path 3-way merge for JSON catalogs (i18n): different keys merge automatically, same-key edits conflict loudly, structural collapses resolve toward *ours* wholesale — never corrupt JSON, never silent loss (empty dicts are leaves). |
| `tools/train/setup_station.sh` | Creates the train's own full clone ("the station") — a separate clone, not a worktree, because the train must hold `main` at all times. |
| `tools/lib/switchyard_config.py` | The project config layer: loads `switchyard.toml` (env var, repo-toplevel, or `~/.config/switchyard/`, in that order) into a `SwitchyardConfig`. Every field defaults to today's hardcoded behavior, so an absent file changes nothing; a broken one (malformed TOML, unknown key) warns on stderr and falls back to defaults rather than crashing. |
| `tools/lib/config_get.sh` | Bash-side `sy_cfg <key> <default>` — lets the guards read one config value without paying a Python startup cost when no config file exists at all. |

## Wiring into a project (Claude Code)

1. Clone this repo somewhere stable, e.g. `~/Developer/switchyard`.
2. In the target repo's `.claude/settings.json`, add hooks that call the
   scripts by absolute path: `git_guard.sh` as a `PreToolUse` hook on Bash,
   `wip_status.sh` + `collision_radar.py` in `SessionStart`.
3. Register the JSON merge driver where the train merges:
   `git config merge.jsoncatalog.driver "python3 <path>/tools/train/json_merge_driver.py %O %A %B"`
   plus a `.gitattributes` line `your/catalog.json merge=jsoncatalog`.
4. `bash tools/guards/setup_pueue.sh`, then `bash tools/train/setup_station.sh`.
5. Land branches with
   `python3 tools/train/merge_train.py run --repo <station> [--branch NAME]`
   (default candidates: open non-draft PRs against `main`, priority-labeled
   ones first, then oldest first by PR number).
   Exit codes: 0 = nothing errored (rejected/conflict are normal outcomes),
   1 = dry-run found a branch that would not land, 2 = a system error.

Convention that makes it all work: sessions open **draft** PRs while working
and mark them **ready** to queue for the train; `main` is never pushed by
anyone but the train.

## Configuration

Every tool works with zero config, using the hardcoded defaults this project
shipped with. To override any of them, copy `switchyard.toml.example` to
`switchyard.toml` and uncomment what you need:

```bash
cp switchyard.toml.example switchyard.toml
```

Resolution order (first hit wins) — see `tools/lib/switchyard_config.py`:

1. `$SWITCHYARD_CONFIG` — path to a toml file, used verbatim.
2. `<git toplevel>/switchyard.toml` — the config next to the repo it applies to.
3. `~/.config/switchyard/config.toml` — a machine-wide fallback.
4. All defaults, if none of the above exist.

```toml
[switchyard]
protected_branch = "main"
wip_cap = 8
live_prefixes = ["claude/", "fix/", "feat/", "release/"]
```

An unknown key is ignored with a one-line warning on stderr (forward
compatibility); malformed TOML falls back to defaults with a warning instead
of crashing — a broken config must never brick a guard. See
`switchyard.toml.example` for every key, its default, and what it controls.

## Tests

82 tests, plain pytest, no project dependencies:

```bash
python3 -m pytest tests/ -q
```

`SWITCHYARD_CATALOG=/path/to/your/catalog.json` points the losslessness test
at a real catalog instead of the bundled fixture.

## License

MIT
