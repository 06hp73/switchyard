#!/usr/bin/env bash
# SessionStart summary: how many live (unmerged) tracks exist, WIP cap state.
# Prints one line; never fails the session (always exit 0).
CAP=5
cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0
LIVE=$(git for-each-ref refs/heads --format='%(refname:short)' 2>/dev/null \
  | grep -E '^(claude|fix|feat)/' \
  | while read -r br; do
      n=$(git rev-list --count main.."$br" 2>/dev/null || echo 0)
      [ "${n:-0}" -gt 0 ] && echo "$br"
    done)
COUNT=$(printf '%s' "$LIVE" | grep -c . || true)
if [ "${COUNT:-0}" -gt "$CAP" ]; then
  echo "WIP CAP EXCEEDED: ${COUNT} live unmerged tracks (cap ${CAP}). Do NOT start new tracks; land or explicitly park the oldest first. Live: $(printf '%s' "$LIVE" | tr '\n' ' ')"
else
  echo "WIP: ${COUNT}/${CAP} live tracks."
fi
exit 0
