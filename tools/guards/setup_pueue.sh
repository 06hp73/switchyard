#!/usr/bin/env bash
# One-time (idempotent) pueue setup: machine-wide concurrency caps so 7+
# sessions cannot run two solver-heavy jobs at once on the 8GB host.
set -eu
if ! command -v pueue >/dev/null 2>&1; then
  echo "pueue not found - installing via Homebrew..."
  brew install pueue
fi
pueued -d 2>/dev/null || true
sleep 1
pueue group add solver 2>/dev/null || true
pueue group add train 2>/dev/null || true
pueue parallel 1 -g solver
pueue parallel 1 -g train
pueue group
echo "pueue ready: groups 'solver' and 'train' capped at 1 concurrent job."
