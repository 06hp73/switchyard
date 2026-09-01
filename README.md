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
| `tools/guards/git_guard.sh` | PreToolUse hook: blocks `git stash` (repo-global across worktrees — silently destroys other sessions' work), identity changes (`git config user.*`, `-c`, `--author`, env overrides), force pushes (incl. `+refspec`), any push to `main`, and `rm -rf` on worktree dirs. Fail-open on parse errors. Best-effort text matching, hardened against realistic accidents — see "Enforcement model" below for why it is not, and cannot be, adversarially complete. |
| `tools/guards/pre_push_hook.sh`, `tools/guards/install_pre_push.sh` | A real git `pre-push` hook plus its installer — the robust local enforcement layer for the protected branch, immune to the shell-text tricks that beat `git_guard.sh` because it reads git's own already-resolved refs instead of parsing command text. See "Enforcement model" below. |
| `tools/guards/wip_status.sh` | Session-start banner: live (unmerged) track count vs the WIP cap. |
| `tools/guards/worktree_env.sh` | Deterministic per-worktree port + Redis-DB allocation. |
| `tools/guards/setup_pueue.sh` | pueue groups `solver`/`train` capped at 1 concurrent job machine-wide. |
| `tools/radar/collision_radar.py` | In-memory pairwise merge replay (`git merge-tree --write-tree`) across all live branches: a collision map hours before the collision. |
| `tools/train/merge_train.py` | The merge train — the only writer to `main`. One branch at a time: refresh, replay-check, real merge, run the gate on the combined tree, push only on green. Gate-identity cache (a dry-run or cheap gate can never pre-approve a real run), process-group kill on gate timeout, kernel-managed flock lease (auto-freed if the holder dies, nothing to reclaim), per-branch error containment. Red never moves `main` and never blocks the queue. |
| `tools/train/gate.sh`, `tools/train/gate_full.sh` | The gate definitions: `gate.sh` (fast tier, default) runs lint + fast suite + machine-local characterization goldens; `gate_full.sh` (full tier, select per-branch via `--gate` for branches touching optimizer math) additionally runs the analytic golden-master suite and the optimizer fuzz/superadditivity/certificate proofs the fast tier excludes. Both pin the tested tree to the station checkout via an import preflight. Adapt to your project. |
| `tools/train/json_merge_driver.py` | Key-path 3-way merge for JSON catalogs (i18n): different keys merge automatically, same-key edits conflict loudly, structural collapses resolve toward *ours* wholesale — never corrupt JSON, never silent loss (empty dicts are leaves). |
| `tools/train/setup_station.sh` | Creates the train's own full clone ("the station") — a separate clone, not a worktree, because the train must hold `main` at all times. |
| `tools/lib/switchyard_config.py` | The project config layer: loads `switchyard.toml` (env var, repo-toplevel, or `~/.config/switchyard/`, in that order) into a `SwitchyardConfig`. Every field defaults to today's hardcoded behavior, so an absent file changes nothing; a broken one (malformed TOML, unknown key) warns on stderr and falls back to defaults rather than crashing. |
| `tools/lib/config_get.sh` | Bash-side `sy_cfg <key> <default>` — lets the guards read one config value without paying a Python startup cost when no config file exists at all. |
| `tools/cli.py`, `bin/switchyard` | The unified `switchyard` CLI: `status` (one composed, read-only view — WIP, radar, queue via `gh`, flaky log if present, last landings), `stats` (counts, landing rate, gate-time mean/p90, top rejected branches from `.train/history.jsonl`), and thin `radar`/`land` passthroughs to the modules above (every flag forwards verbatim, so behavior never drifts from calling those scripts directly). Every `status`/`stats` section degrades independently — a missing `gh`, an untrained repo, or a missing `.train/` file never takes the rest of the view down. |

## Wiring into a project (Claude Code)

1. Clone this repo somewhere stable, e.g. `~/Developer/switchyard`.
2. In the target repo's `.claude/settings.json`, add hooks that call the
   scripts by absolute path: `git_guard.sh` as a `PreToolUse` hook on Bash,
   `wip_status.sh` + `collision_radar.py` in `SessionStart`.
3. Install the pre-push hook in every repo whose protected branch must
   actually hold: `bash tools/guards/install_pre_push.sh --repo <path>`
   (default: symlinked, so it upgrades whenever this checkout does; see its
   `--mode copy`/`--force`/`--chain` for a repo that already has a
   pre-push hook). This is the layer that still holds even when a shell
   trick fools step 2's guard — see "Enforcement model" below.
4. Register the JSON merge driver where the train merges:
   `git config merge.jsoncatalog.driver "python3 <path>/tools/train/json_merge_driver.py %O %A %B"`
   plus a `.gitattributes` line `your/catalog.json merge=jsoncatalog`.
5. `bash tools/guards/setup_pueue.sh`, then `bash tools/train/setup_station.sh`.
6. Land branches with
   `python3 tools/train/merge_train.py run --repo <station> [--branch NAME]`
   (default candidates: open non-draft PRs against `main`, priority-labeled
   ones first, then oldest first by PR number), or the equivalent
   `bin/switchyard land --repo <station> [--branch NAME]` — every flag
   forwards straight to `merge_train.py run`.
   Exit codes: 0 = nothing errored (rejected/conflict are normal outcomes),
   1 = dry-run found a branch that would not land, 2 = a system error.

## Enforcement model (defense in depth)

Three separate layers stand between a session and the protected branch.
They are not redundant copies of each other — each covers what the layer
below it structurally cannot — and none of the three is, by itself, a
security boundary:

1. **`git_guard.sh` (the `PreToolUse` hook) is best-effort accident
   prevention for a cooperative agent session, nothing more.** It
   pattern-matches the TEXT of a Bash tool call before that command runs.
   Text matching cannot be made adversarially complete: enough shell
   cleverness — a quote placed mid-word, a backslash, a
   `GIT_DIR=`/`GIT_WORK_TREE=` redirection, an alias, a line continuation,
   `$(...)` substitution — constructs a command whose text reads as
   something harmless while resolving to exactly the banned push. This
   guard is hardened against the realistic, plausible mistakes a
   cooperative agent could actually type (its own comments and
   `tests/test_git_guard.py` track each one), and deliberately NOT chased
   toward completeness against a determined adversary, because that is the
   wrong tool for that job. Do not rely on it to stop a determined shell —
   it exists to catch an honest mistake before it happens, cheaply, for
   every session, on every command.
2. **`pre_push_hook.sh` (installed by `install_pre_push.sh`, step 3 above)
   is the robust LOCAL layer.** It is a real git `pre-push` hook, so it
   runs INSIDE git itself, after git has already resolved every ref and
   remote the push actually touches. By the time it sees a ref, every env
   var, quote, backslash, and redirection flag from the original command
   line has already been expanded and interpreted by git — not re-parsed,
   approximately, by this hook's own text matching. It catches what layer
   1 structurally cannot (`tests/test_pre_push_hook.py` proves this against
   a real bare repo, including one of the exact bypasses that defeats
   layer 1). Its own honest limits: `git push --no-verify` skips any
   client-side git hook outright, and a hook only protects the one repo it
   is actually installed into. Neither of those is a gap specific to this
   hook — every local git hook that has ever existed shares them.
3. **Server-side branch protection (a ruleset on the remote host — required
   status checks, restrict-who-can-push, block force-pushes and direct
   pushes) is the actual wall.** It is the only layer of the three a pusher
   cannot opt out of from their own client, because nothing about it runs
   on their machine at all. Layers 1 and 2 exist to catch a mistake BEFORE
   it reaches the server — a faster, friendlier failure for a session to
   hit than a rejected push — but the server ruleset is what actually holds
   if either of them is missing, disabled, or bypassed (`--no-verify`, an
   uninstalled hook, a guard fooled by its own text parsing). Keep the
   server ruleset on regardless of whether layers 1 and 2 are installed;
   install layer 2 everywhere it matters as defense in depth on top of
   layer 1, never as a substitute for layer 3.

Convention that makes it all work: sessions open **draft** PRs while working
and mark them **ready** to queue for the train; `main` is never pushed by
anyone but the train.

## The `switchyard` CLI

`bin/switchyard` is a single entry point over the scripts above (add it to
`PATH`, or call it by path). It resolves its own interpreter via
`tools/lib/resolve_python.sh` (the same resolver the bash guards use for
their own config reads): `$SWITCHYARD_PYTHON` if set and verified ≥ 3.11,
else the first of `python3.13`/`python3.12`/`python3.11` found on `PATH`,
else bare `python3` only if it verifies as ≥ 3.11 too. If none of those
resolve, `bin/switchyard` exits 1 with a clear stderr message pointing at
`SWITCHYARD_PYTHON`, rather than silently running under whatever older
`python3` it found — unlike the bash guards' own config reads, which warn on
stderr and fall back to their hardcoded default value instead of crashing,
since a guard must never take a session down.

```bash
bin/switchyard status --repo <station>   # one composed view: WIP, radar, queue, flaky, last landings
bin/switchyard stats  --repo <station>   # .train/history.jsonl: counts, landing rate, gate-time mean/p90
bin/switchyard radar  --repo <station>   # passthrough - identical to collision_radar.py's own flags
bin/switchyard land   --repo <station>   # passthrough - identical to `merge_train.py run`'s own flags
bin/switchyard track new <name>          # branch + worktree from `worktree_dir`/`branch_prefix` + draft PR
bin/switchyard track done <name>         # verify the PR merged (or --force-local), then remove worktree + branches
bin/switchyard watch install             # opt-in launchd agent: periodic `land` (macOS, off by default)
bin/switchyard watch uninstall           # unload + remove that agent
bin/switchyard watch status              # is it installed, is it loaded
bin/switchyard propose-revert <sha>      # branch + draft PR that reverts <sha> - proposes only, never lands it
```

`status` and `stats` are read-only and safe to run anytime, including
against a repo that has never trained: each section (WIP/RADAR/QUEUE/FLAKY/
LAST LANDINGS) reports its own "nothing here yet" line instead of failing
the whole view when its data source (`gh`, `.train/history.jsonl`, ...) is
absent.

Both resolve `--repo` the same way: an explicit `--repo` always wins,
otherwise they prefer the configured `[switchyard].station` over the bare
working directory, since train state almost always lives in the station
clone, not wherever a human happens to be sitting when they type the
command. Both always print the resolved `repo:` path and whether `.train/`
exists there as their first lines, so an empty view is never mistaken for
"nothing has happened" instead of "looked in the wrong directory."

`track new`/`track done` need `worktree_dir` set in `switchyard.toml`
(`track new` errors with a clear message otherwise); `branch_prefix`
defaults to `"claude/"`. Without `gh` on `PATH`, `track new` still creates
the branch, worktree, and push, and prints the `gh pr create` command to
run by hand later; `track done` without `gh` needs `--force-local` (it
otherwise refuses to clean up a branch it cannot confirm was merged).
`track done`'s local-branch delete is `-D`, not `-d`, on purpose: this
repo's own convention is squash-merge onto the protected branch, so a
landed track branch's commits never become reachable from its ancestry —
`git branch -d`'s "is this merged" check would refuse every track branch by
design, even ones that landed cleanly.

`watch install` writes and loads a per-repo `~/Library/LaunchAgents/
com.switchyard.<reponame>.plist` that runs `bin/switchyard land --repo
<station> --land pr-squash --batch <cfg.batch>` every `--interval` seconds
(default 1200 = 20 minutes) — pr-squash, not push, because an unattended
watcher must land through the same server-side ruleset a human-reviewed PR
would. It refuses with a clear message if `station` is unset in
`switchyard.toml`, and every `watch` subcommand takes `--dry-run` to print
what it would do (the plist XML for `install`, the unload/remove actions
for `uninstall`) without touching `launchctl` or the filesystem. Nothing
installs this automatically; it is opt-in only.

`propose-revert <sha>` fetches, branches `revert-<sha7>` off the protected
branch's tip, runs `git revert --no-edit <sha>`, pushes it, and (if `gh` is
available) opens it as a **draft** pull request — `--reason FILE`'s content,
if given, is embedded verbatim as fenced data under an "Automated failure
context" heading, never interpreted. Owner policy: this command proposes a
revert, it never lands one — it never marks the PR ready and never merges
it, so an automated failure-triage flow can call it safely without any risk
of an unattended revert reaching `main` on its own. A revert that conflicts
aborts cleanly and leaves no branch behind (exit 2); without `gh`, the
branch is still pushed and the `gh pr create` command is printed to run by
hand.

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

243 tests, plain pytest, no project dependencies (needs Python 3.11+ for
`tomllib` — see `tools/lib/switchyard_config.py`):

```bash
python3 -m pytest tests/ -q
```

`SWITCHYARD_CATALOG=/path/to/your/catalog.json` points the losslessness test
at a real catalog instead of the bundled fixture.

## License

MIT
