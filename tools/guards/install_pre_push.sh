#!/usr/bin/env bash
# Installs tools/guards/pre_push_hook.sh as the pre-push hook of a target
# repo - see README.md's "Enforcement model" section for why this hook
# exists alongside git_guard.sh (the PreToolUse guard): that one pattern-
# matches shell command text and is beatable; this one runs inside git
# itself, after every ref is already resolved, and cannot be fooled the
# same way.
#
# Usage:
#   install_pre_push.sh [--repo PATH] [--mode symlink|copy] [--force] [--chain]
#
#   --repo PATH   Target repo to install into. Default: current directory.
#                 Works with a plain .git/hooks layout, a repo that has
#                 core.hooksPath configured, and a linked worktree (hooks
#                 are shared repo-wide in git, never per-worktree, and this
#                 always resolves to that one shared location).
#   --mode        symlink (default): the installed hook is a symlink back to
#                 THIS checkout's pre_push_hook.sh, so a later `git pull`
#                 here upgrades every repo it's installed into automatically.
#                 copy: a standalone snapshot is written instead - use this
#                 when the target repo must not depend on this checkout
#                 still existing at the same path (e.g. it might be deleted,
#                 or the target repo is redistributed on its own). A copy
#                 still reads switchyard.toml/~/.config/switchyard normally;
#                 install bakes in this checkout's tools/lib path for it.
#   --force       If a DIFFERENT (non-switchyard) pre-push hook already
#                 exists at the target, back it up next to itself as
#                 "pre-push.pre-switchyard-backup" and overwrite it. The
#                 backed-up hook stops running.
#   --chain       Like --force, but the backed-up hook keeps running too:
#                 installs a small wrapper that runs the switchyard guard
#                 FIRST (so it can still reject a protected-branch push
#                 outright) and, only once that passes, falls through to the
#                 original hook. If there is nothing to chain to (no
#                 existing hook, and no backup from an earlier install),
#                 this degrades to a plain install with a note - there is
#                 nothing for it to preserve.
#
# Idempotent: running the same invocation twice in a row succeeds both
# times and converges to the same end state - re-running never needs
# --force/--chain to get past a hook this same script already installed.
#
# Refuses clearly (nonzero exit, no filesystem changes) if a pre-push hook
# this script did not install is already present and neither --force nor
# --chain was given.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_HOOK="$SCRIPT_DIR/pre_push_hook.sh"
LIB_DIR_ABS="$(cd "$SCRIPT_DIR/../lib" && pwd)"

MARKER_PLAIN='# switchyard-guard: pre-push v1'
MARKER_CHAIN='# switchyard-guard: pre-push-chain-wrapper v1'
BACKUP_NAME='pre-push.pre-switchyard-backup'
SIDECAR_NAME='pre-push.switchyard'

REPO="$(pwd)"
MODE="symlink"
FORCE=0
CHAIN=0

usage() {
  echo "usage: $(basename "$0") [--repo PATH] [--mode symlink|copy] [--force] [--chain]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)
      [ $# -ge 2 ] || { echo "error: --repo needs a value" >&2; exit 2; }
      REPO="$2"
      shift 2
      ;;
    --mode)
      [ $# -ge 2 ] || { echo "error: --mode needs a value" >&2; exit 2; }
      MODE="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --chain)
      CHAIN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unrecognized argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

case "$MODE" in
  symlink|copy) : ;;
  *)
    echo "error: --mode must be 'symlink' or 'copy', got '$MODE'" >&2
    exit 2
    ;;
esac

if [ ! -f "$REAL_HOOK" ]; then
  echo "error: $REAL_HOOK not found (this script must sit next to pre_push_hook.sh)" >&2
  exit 2
fi

if ! RAW_HOOKS_PATH=$(git -C "$REPO" rev-parse --git-path hooks 2>/dev/null); then
  echo "error: '$REPO' is not a git repository" >&2
  exit 2
fi
REPO="$(cd "$REPO" && pwd)"
case "$RAW_HOOKS_PATH" in
  /*) HOOKS_DIR="$RAW_HOOKS_PATH" ;;
  *) HOOKS_DIR="$REPO/$RAW_HOOKS_PATH" ;;
esac
mkdir -p "$HOOKS_DIR"
HOOKS_DIR="$(cd "$HOOKS_DIR" && pwd)"
HOOK_PATH="$HOOKS_DIR/pre-push"
SIDECAR="$HOOKS_DIR/$SIDECAR_NAME"
BACKUP="$HOOKS_DIR/$BACKUP_NAME"

# copy_hook_with_override SRC DEST LIBDIR - a byte-for-byte copy of SRC
# except the one line matching SWITCHYARD_LIB_DIR_OVERRIDE="" (pre_push_
# hook.sh's own hook for this) is rewritten to point at LIBDIR. Uses awk's
# -v (a plain string assignment, not a regex) so LIBDIR's own characters -
# slashes, anything - never need escaping the way a sed replacement would.
copy_hook_with_override() {
  local src="$1" dest="$2" libdir="$3"
  awk -v lib="$libdir" '
    $0 == "SWITCHYARD_LIB_DIR_OVERRIDE=\"\"" { print "SWITCHYARD_LIB_DIR_OVERRIDE=\"" lib "\""; next }
    { print }
  ' "$src" > "$dest"
}

# install_hook_file DEST - writes the switchyard guard itself to DEST per
# --mode, and makes it executable.
install_hook_file() {
  local dest="$1"
  if [ "$MODE" = "symlink" ]; then
    ln -sf "$REAL_HOOK" "$dest"
  else
    copy_hook_with_override "$REAL_HOOK" "$dest" "$LIB_DIR_ABS"
    chmod +x "$dest"
  fi
}

write_chain_wrapper() {
  local dest="$1"
  cat > "$dest" <<'EOF'
#!/bin/sh
# switchyard-guard: pre-push-chain-wrapper v1
# Installed by install_pre_push.sh --chain: this repo already had a
# pre-push hook before switchyard was installed. Rather than silently
# discarding it, install_pre_push.sh moved it to
# "pre-push.pre-switchyard-backup" next to this file and put the switchyard
# guard itself at "pre-push.switchyard" - this wrapper runs the switchyard
# guard FIRST (so it can still reject a protected-branch push outright)
# and, only if that passes, falls through to the original hook so its own
# checks keep applying too. Re-running install_pre_push.sh --chain
# regenerates this file and "pre-push.switchyard" in place without
# touching the backup.
HOOK_DIR=$(cd "$(dirname "$0")" && pwd)
STDIN_TMP=$(mktemp 2>/dev/null) || STDIN_TMP="/tmp/switchyard-pre-push-stdin.$$"
trap 'rm -f "$STDIN_TMP"' EXIT INT TERM
cat > "$STDIN_TMP"

"$HOOK_DIR/pre-push.switchyard" "$@" < "$STDIN_TMP"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  exit "$STATUS"
fi

if [ -x "$HOOK_DIR/pre-push.pre-switchyard-backup" ]; then
  "$HOOK_DIR/pre-push.pre-switchyard-backup" "$@" < "$STDIN_TMP"
  exit $?
fi

exit 0
EOF
  chmod +x "$dest"
}

# --- classify what (if anything) is currently at $HOOK_PATH ----------------
EXISTING_KIND="none"
if [ -L "$HOOK_PATH" ] && [ "$(readlink "$HOOK_PATH")" = "$REAL_HOOK" ]; then
  EXISTING_KIND="ours-plain"
elif [ -e "$HOOK_PATH" ] && grep -qF "$MARKER_CHAIN" "$HOOK_PATH" 2>/dev/null; then
  EXISTING_KIND="ours-chain"
elif [ -e "$HOOK_PATH" ] && grep -qF "$MARKER_PLAIN" "$HOOK_PATH" 2>/dev/null; then
  EXISTING_KIND="ours-plain"
elif [ -e "$HOOK_PATH" ] || [ -L "$HOOK_PATH" ]; then
  EXISTING_KIND="foreign"
fi

if [ "$EXISTING_KIND" = "foreign" ] && [ "$FORCE" -eq 0 ] && [ "$CHAIN" -eq 0 ]; then
  {
    echo "refusing to overwrite an existing pre-push hook that switchyard did not install:"
    echo "  $HOOK_PATH"
    echo ""
    echo "Re-run with:"
    echo "  --force            back it up to $BACKUP_NAME and overwrite it (it stops running)"
    echo "  --chain            back it up and keep running it too, AFTER the switchyard guard"
  } >&2
  exit 1
fi

WANT_CHAIN=0
if [ "$CHAIN" -eq 1 ]; then
  if [ "$EXISTING_KIND" = "foreign" ] || [ "$EXISTING_KIND" = "ours-chain" ] || [ -e "$BACKUP" ]; then
    WANT_CHAIN=1
  else
    echo "note: --chain given but there is no existing hook (and no prior backup) to chain to - installing directly." >&2
  fi
fi

if [ "$EXISTING_KIND" = "foreign" ]; then
  if [ -e "$BACKUP" ]; then
    echo "error: $BACKUP already exists and would be overwritten by backing up the current hook." >&2
    echo "Move or remove it by hand first, then re-run this installer." >&2
    exit 2
  fi
  mv "$HOOK_PATH" "$BACKUP"
  echo "backed up existing pre-push hook to $BACKUP"
fi

if [ "$WANT_CHAIN" -eq 1 ]; then
  install_hook_file "$SIDECAR"
  write_chain_wrapper "$HOOK_PATH"
  KIND_DESC="chained ($SIDECAR_NAME + your previous hook at $BACKUP_NAME, in that order)"
else
  install_hook_file "$HOOK_PATH"
  KIND_DESC="$MODE"
fi

echo "installed switchyard pre-push guard:"
echo "  repo:  $REPO"
echo "  hook:  $HOOK_PATH"
echo "  kind:  $KIND_DESC"
echo ""
echo "verify it is wired up (does not touch any real ref):"
echo "  cd $REPO && printf 'refs/heads/x 0000000000000000000000000000000000000000 refs/heads/main 0000000000000000000000000000000000000000\\n' | sh '$HOOK_PATH' origin \"\$(git remote get-url origin 2>/dev/null || echo none)\""
echo "  (expect: a 'Blocked by pre-push hook' message on stderr and a nonzero exit - if your protected branch isn't"
echo "   named 'main', substitute it in the line above)"
