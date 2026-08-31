#!/usr/bin/env bash
# SessionStart summary: how many live (unmerged) tracks exist, WIP cap state.
# Prints one line; never fails the session (always exit 0).
CAP=5
cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0
LIVE=$(git for-each-ref refs/heads --format='%(refname:short)' 2>/dev/null \
  | grep -E '^(claude|fix|feat)/' \
  | while read -r br; do
      n=$(git rev-list --count main.."$br" 2>/dev/null || echo 0)
      # A squash-merged branch keeps commits forever "unmerged" by ancestry
      # (rev-list above stays > 0), even though its content already sits on
      # main under main's own commit. Direct tip-vs-tip `git diff --quiet
      # main "$br"` (no dots - for diff, "A..B" means the same as "A B";
      # only three-dot switches to the merge-base form, which stays
      # non-empty forever here and would miss this case) goes quiet once
      # main already carries everything the branch has to offer.
      if [ "${n:-0}" -gt 0 ] && ! git diff --quiet main "$br" 2>/dev/null; then
        echo "$br"
      fi
    done)
COUNT=$(printf '%s' "$LIVE" | grep -c . || true)
if [ "${COUNT:-0}" -gt "$CAP" ]; then
  echo "WIP CAP EXCEEDED: ${COUNT} live unmerged tracks (cap ${CAP}). Do NOT start new tracks; land or explicitly park the oldest first. Live: $(printf '%s' "$LIVE" | tr '\n' ' ')"
else
  echo "WIP: ${COUNT}/${CAP} live tracks."
fi
exit 0
