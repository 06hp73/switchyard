#!/usr/bin/env bash
# SessionStart summary: how many live (unmerged) tracks exist, WIP cap state.
# Prints one line; never fails the session (always exit 0).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config_get.sh
source "$SCRIPT_DIR/../lib/config_get.sh"
CAP=$(sy_cfg wip_cap 5)
# live_prefixes comes back comma-joined, e.g. "claude/,fix/,feat/" - turned
# into an anchored alternation. Functionally identical to the old hardcoded
# '^(claude|fix|feat)/' (a prefix immediately followed by '/' either way).
PREFIXES=$(sy_cfg live_prefixes "claude/,fix/,feat/")
PREFIX_RE="^($(printf '%s' "$PREFIXES" | sed 's/,/|/g'))"
# protected_branch is advisory here, not guard-scoping like git_guard.sh's
# main-push ban (see sy_cfg_trusted there): this banner only ever prints
# information, it never blocks anything, so a repo-local switchyard.toml is
# allowed to point it at this repo's own trunk name like any other setting.
PROTECTED=$(sy_cfg protected_branch "main")
cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0
LIVE=$(git for-each-ref refs/heads --format='%(refname:short)' 2>/dev/null \
  | grep -E "$PREFIX_RE" \
  | while read -r br; do
      n=$(git rev-list --count "$PROTECTED".."$br" 2>/dev/null || echo 0)
      # A squash-merged branch keeps commits forever "unmerged" by ancestry
      # (rev-list above stays > 0), even though its content already sits on
      # the protected branch under its own commit. Direct tip-vs-tip
      # `git diff --quiet "$PROTECTED" "$br"` (no dots - for diff, "A..B"
      # means the same as "A B"; only three-dot switches to the merge-base
      # form, which stays non-empty forever here and would miss this case)
      # goes quiet once the protected branch already carries everything the
      # branch has to offer.
      if [ "${n:-0}" -gt 0 ] && ! git diff --quiet "$PROTECTED" "$br" 2>/dev/null; then
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
