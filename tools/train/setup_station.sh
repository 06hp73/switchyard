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
# NB: never install the pre-push hook (tools/guards/install_pre_push.sh) on the
# station. The station IS the legitimate writer of the protected branch — the
# train pushes it here. The pre-push hook belongs on WORK checkouts (the main
# dev clone and its worktrees), where it stops sessions from pushing main. In
# pr-squash mode the station never pushes the protected branch locally at all.
echo "station ready at $STATION"
echo "run the train:  cd $STATION && pueue add -g train -- /Users/storslasken/Developer/EV4XL-SIM/.venv/bin/python tools/train/merge_train.py run --repo $STATION"
