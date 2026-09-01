#!/bin/sh
# switchyard-guard: pre-push v1
#
# Git pre-push hook - the ROBUST enforcement layer for the protected branch.
#
# git_guard.sh (the PreToolUse hook next to this file) pattern-matches the
# shell command TEXT of a Bash tool call before it ever runs. That is
# fundamentally beatable: enough shell cleverness (env var indirection,
# quoting, GIT_DIR=, aliases, line continuations, ...) can construct a
# command whose text the guard fails to classify as "a push to main" even
# though it resolves to exactly that. See README.md's "Enforcement model"
# section for the full reasoning and the honest limits of that layer.
#
# This hook closes that gap differently: it runs INSIDE git itself, after
# git has already fully resolved every ref and remote involved in the push.
# Git invokes a pre-push hook as:
#
#   <hook> <remote-name> <remote-url>
#
# and feeds stdin lines of the form:
#
#   <local ref> SP <local oid> SP <remote ref> SP <remote oid> LF
#
# (see .git/hooks/pre-push.sample for git's own reference copy of this
# contract). By the time this hook runs, every env var, alias, quoting
# trick, and redirection flag from the original command line has already
# been expanded and interpreted BY GIT ITSELF - there is no shell-string
# bypass left at this layer. Whatever arrives on stdin as <remote ref> is
# the ACTUAL ref git is about to update, in full "refs/heads/..." form, no
# matter how the push was typed (quoted, aliased, GIT_DIR-redirected, ...).
#
# Exit 0 = allow the push. Exit nonzero = abort it - nothing is sent to the
# remote for this push invocation.
#
# LIMITS, stated plainly (see README.md's Enforcement model section):
#   - `git push --no-verify` skips this hook entirely. Any CLIENT-SIDE git
#     hook can always be bypassed by whoever controls that client - that is
#     true of every hook in .git/hooks, not a gap specific to this one.
#   - This hook only protects whichever repo/remote it is actually installed
#     into (see install_pre_push.sh). It says nothing about a push made from
#     a checkout where it was never installed.
#   - The only enforcement a pusher cannot opt out of is server-side: a
#     branch-protection ruleset on the remote host. Install this hook as
#     defense in depth; keep the server-side ruleset on regardless.
#
# POSIX sh only, deliberately - a hook installed via core.hooksPath or
# copied into another repo's .git/hooks may be run by a /bin/sh that is not
# bash (e.g. dash on Debian/Ubuntu). No arrays, no [[ ]], no bash-only
# parameter expansions.

REMOTE_URL=${2:-}

# --- locate this script's real directory, even through a symlink install ----
# install_pre_push.sh's default install mode symlinks <repo>/.git/hooks/
# pre-push straight at this file. Git execs that symlink directly (the
# kernel follows it to open this file's bytes), but $0 is still the SYMLINK
# path as git invoked it, not this file's real location - a plain
# dirname "$0" would resolve to the TARGET repo's .git/hooks, not this
# checkout's tools/guards, and the sibling ../lib/config_get.sh lookup below
# would miss entirely. So: resolve $0 through any symlink chain by hand
# first (POSIX readlink has no -f flag - that's a GNU extension not
# guaranteed on e.g. macOS/BSD - hence the manual loop instead of
# `readlink -f`).
_resolve_self() {
  _target=$1
  while [ -L "$_target" ]; do
    _link=$(readlink "$_target")
    case "$_link" in
      /*) _target=$_link ;;
      *) _target=$(dirname "$_target")/$_link ;;
    esac
  done
  printf '%s\n' "$_target"
}
SELF=$(_resolve_self "$0")
SCRIPT_DIR=$(cd "$(dirname "$SELF")" && pwd)

# A "copy" install (install_pre_push.sh --mode copy) places a standalone
# copy of this file elsewhere with no sibling ../lib at all - resolving the
# symlink chain above cannot help there because there IS no symlink, just an
# independent file. install_pre_push.sh bakes this checkout's real lib
# directory into copies by rewriting the line below in place (see its
# copy_hook_with_override); a direct run or a symlink install leaves it
# empty and finds ../lib from SCRIPT_DIR instead, which is already correct
# in both of those cases.
SWITCHYARD_LIB_DIR_OVERRIDE=""

LIB_DIR="$SCRIPT_DIR/../lib"
if [ -n "$SWITCHYARD_LIB_DIR_OVERRIDE" ] && [ -f "$SWITCHYARD_LIB_DIR_OVERRIDE/config_get.sh" ]; then
  LIB_DIR="$SWITCHYARD_LIB_DIR_OVERRIDE"
fi

# Safe fallback if config_get.sh cannot be found at all (e.g. a copy install
# whose override above never got rewritten, or was separated from tools/lib
# entirely): protect the literal default (main / always-enforce) rather than
# silently doing nothing. Sourcing config_get.sh below, when found, replaces
# this with the real sy_cfg_trusted.
sy_cfg_trusted() { printf '%s\n' "$2"; }
if [ -f "$LIB_DIR/config_get.sh" ]; then
  # shellcheck source=../lib/config_get.sh
  . "$LIB_DIR/config_get.sh"
fi

# Same guard-scoping keys git_guard.sh reads, same trusted-only resolution -
# a repo-local switchyard.toml must never be able to retarget or disarm the
# hook protecting its own repo (see config_get.sh's docstring). Unlike
# git_guard.sh, an EMPTY product_remote_match here means "always enforce",
# NOT "fall back to the EV4SIM hardcoded default": git_guard.sh is one
# PreToolUse hook watching bash commands from EVERY repo a session might sit
# in, so it needs a hardcoded product-repo default to avoid falsely
# protecting unrelated repos (e.g. switchyard's own main, see git_guard.sh's
# own comment on this). This hook is installed ONE REPO AT A TIME, on
# purpose, by install_pre_push.sh - if you installed it here, you meant to
# protect THIS repo's protected branch, regardless of what its remote is
# named.
PROTECTED_BRANCH=$(sy_cfg_trusted protected_branch main)
PRODUCT_MATCH=$(sy_cfg_trusted product_remote_match "")

is_product_remote() {
  [ -z "$PRODUCT_MATCH" ] && return 0
  case "$1" in
    *"$PRODUCT_MATCH"*) return 0 ;;
    *) return 1 ;;
  esac
}

BLOCKED=0
# shellcheck disable=SC2034 # LOCAL_SHA/REMOTE_SHA are positional fields in
# git's stdin contract (see .git/hooks/pre-push.sample); only REMOTE_REF
# drives this check, but a real 4-field line still needs all four names to
# split correctly instead of jamming the tail into REMOTE_REF.
while read -r LOCAL_REF LOCAL_SHA REMOTE_REF REMOTE_SHA; do
  [ -z "$LOCAL_REF" ] && continue
  if [ "$REMOTE_REF" = "refs/heads/$PROTECTED_BRANCH" ] && is_product_remote "$REMOTE_URL"; then
    printf 'Blocked by pre-push hook: %s is protected on this remote.\n' "$REMOTE_REF" >&2
    printf 'Direct pushes, force-pushes, and deletes are reserved for the merge train.\n' >&2
    printf 'Push your feature branch and land it through the switchyard train instead.\n' >&2
    BLOCKED=1
  fi
done

if [ "$BLOCKED" = "1" ]; then
  exit 1
fi
exit 0
