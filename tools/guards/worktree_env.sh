#!/usr/bin/env bash
# Deterministic per-worktree resource slots. Usage:
#   eval "$(bash tools/guards/worktree_env.sh)"
# Name source: $EV4XL_WORKTREE_NAME, else the basename of the git toplevel.
# Hash the name into slot 0-99: dash 8100+slot, api 8500+slot, redis db slot%16.
# 8050 (the default Dash port) is never allocated, per the dash-preview rule.
set -eu
NAME="${EV4XL_WORKTREE_NAME:-$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")}"
HASH=$(printf '%s' "$NAME" | cksum | cut -d' ' -f1)
SLOT=$((HASH % 100))
DASH=$((8100 + SLOT))
[ "$DASH" -eq 8050 ] && DASH=8199
printf 'export EV4XL_PORT_OFFSET="%s"\n' "$SLOT"
printf 'export EV4XL_DASH_PORT="%s"\n' "$DASH"
printf 'export EV4XL_API_PORT="%s"\n' "$((8500 + SLOT))"
printf 'export EV4XL_REDIS_DB="%s"\n' "$((SLOT % 16))"
