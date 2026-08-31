#!/usr/bin/env bash
# One-time setup of the train's own clone ("the station").
# A separate clone, not a worktree: the train needs main checked out at all
# times, and linked worktrees cannot hold a branch another worktree holds.
set -eu
STATION="${EV4XL_STATION:-$HOME/ev4xl-train-station}"
ORIGIN_URL=$(git -C /Users/storslasken/Developer/EV4XL-SIM remote get-url origin)
if [ ! -d "$STATION/.git" ]; then
  git clone "$ORIGIN_URL" "$STATION"
fi
cd "$STATION"
git checkout main
git pull --ff-only
if [ -f tools/train/setup_merge_driver.sh ]; then
  bash tools/train/setup_merge_driver.sh
else
  echo "merge-driver setup skipped (train tools not on main yet - commissioning mode)"
fi
mkdir -p .train
echo "station ready at $STATION"
echo "run the train:  cd $STATION && pueue add -g train -- /Users/storslasken/Developer/EV4XL-SIM/.venv/bin/python tools/train/merge_train.py run --repo $STATION"
