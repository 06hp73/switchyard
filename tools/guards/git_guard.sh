#!/usr/bin/env bash
# PreToolUse guard for Bash commands in Claude Code sessions.
# Blocks git operations that are unsafe when many sessions share one repo:
#   - git stash (repo-global refs/stash silently destroys other sessions' work;
#     four independent incident reports; CLAUDE.md mandates WIP commits instead)
#   - git config user.* (identity drift breaks deploy author validation silently)
#   - force pushes
#   - any push to main IN THE PRODUCT REPO (main is written exclusively by the
#     merge train there; other repos are out of scope, see below)
#   - rm -rf on worktree directories (orphans git metadata; use git worktree remove)
# Exit 0 = allow, exit 2 = block with reason on stderr.
# Fail-open on parse errors: a broken guard must never paralyze sessions.

INPUT=$(cat 2>/dev/null) || exit 0
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -z "$CMD" ] && exit 0
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config_get.sh
source "$SCRIPT_DIR/../lib/config_get.sh"

# The main-push ban below is scoped to the product repo (06hp73/EV4SIM) only:
# switchyard is itself a standalone repo (github.com/06hp73/switchyard) whose
# own main has no branch-protection ruleset, and its merge train pushes main
# directly there - a global ban would wrongly block that legitimate push too.
# Identity is decided by cwd's `origin` remote URL, not by path shape (a
# worktree of either repo can live anywhere). Fail-safe: if cwd is missing,
# not a git repo, or has no resolvable `origin`, the ban STAYS ENFORCED - an
# unknown repo is treated as the protected one, never the reverse.
MAIN_PUSH_GUARD_ACTIVE=1
if [ -n "$CWD" ] && ORIGIN_URL=$(git -C "$CWD" remote get-url origin 2>/dev/null) \
   && [ -n "$ORIGIN_URL" ]; then
  # switchyard.toml's product_remote_match overrides which origin substring
  # identifies "the protected product repo"; empty (the default when
  # unconfigured) keeps this hardcoded fallback so existing behavior is
  # unchanged with no config file present.
  PRODUCT_MATCH=$(sy_cfg product_remote_match "")
  [ -z "$PRODUCT_MATCH" ] && PRODUCT_MATCH="06hp73/EV4SIM"
  case "$ORIGIN_URL" in
    *"$PRODUCT_MATCH"*) : ;;  # the product repo - stays enforced
    *) MAIN_PUSH_GUARD_ACTIVE=0 ;;
  esac
fi

block() {
  echo "Blocked by git_guard: $1" >&2
  exit 2
}

# Normalize: the rtk hook may have prefixed commands with "rtk ".
# NOTE: BSD sed (macOS) does not honor GNU's \b word-boundary escape here —
# it silently fails to match, leaving "rtk " in place. Use BSD's own
# [[:<:]]/[[:>:]] word-boundary classes under -E instead (verified to strip
# a standalone "rtk " token without eating surrounding whitespace, and to
# leave "rtkfoo" untouched).
NORM=$(printf '%s' "$CMD" | sed -E 's/[[:<:]]rtk[[:>:]] //g')

# Whitespace-tolerant git->stash gap: "git  stash" (extra spaces) must still
# match, so the separator is [[:space:]]+, not a literal single space.
if printf '%s' "$NORM" | grep -qE '(^|[;&|]\s*|&&\s*)git[[:space:]]+stash\b'; then
  if ! printf '%s' "$NORM" | grep -qE 'git[[:space:]]+stash[[:space:]]+list'; then
    block "'git stash' is banned — the stash stack is shared across ALL worktrees and silently destroys other sessions' work. Use a temporary WIP commit instead (CLAUDE.md)."
  fi
fi

# Two independent greps joined by shell "||", not one A|B regex: this grep's
# engine has a real bug where combining two full "git ... user\." clauses
# into a single top-level alternation makes the FIRST clause silently stop
# matching (reproduced on this box, not theoretical).
# Second clause: "git -c user.name=..." / "git -c user.email=..." sets
# identity for a single invocation without touching git config at all, so a
# config-only check misses it entirely.
# Third clause: "git commit --author='Name <mail>'" (or space-separated
# "--author Name") overrides the commit author directly — no "config" or
# "-c" token appears anywhere, so neither clause above sees it. The
# (=|[[:space:]]) tail requires an immediate boundary after the literal
# "--author" so it does not fire on some future unrelated "--author-ish" flag.
# Fourth/fifth clauses: GIT_AUTHOR_*/GIT_COMMITTER_* env-var assignments
# override identity for the single invocation they prefix, e.g.
# "GIT_AUTHOR_EMAIL=x git commit" — the assignment sits BEFORE "git" in the
# string and contains no "config"/"-c"/"--author" token, so it needs its own
# check. Kept as two separate greps rather than one "(AUTHOR|COMMITTER)"
# alternation: the bug noted above was only reproduced for the original two
# clauses, but nothing rules out the same class of bug here, so no new
# top-level "|" is introduced.
if printf '%s' "$NORM" | grep -qE 'git[[:space:]]+config\b.*\buser\.(name|email)' \
   || printf '%s' "$NORM" | grep -qE 'git[[:space:]]+.*-c[[:space:]]*user\.(name|email)=' \
   || printf '%s' "$NORM" | grep -qE 'git[[:space:]]+.*--author(=|[[:space:]])' \
   || printf '%s' "$NORM" | grep -qE '\bGIT_AUTHOR_[A-Za-z_]+=' \
   || printf '%s' "$NORM" | grep -qE '\bGIT_COMMITTER_[A-Za-z_]+='; then
  block "changing git identity is banned — commits under an unrecognized identity get deploys rejected silently. Self-credit in the commit body instead."
fi

# Also matches a "+"-prefixed refspec ("git push origin +src:dst"), which is
# force-push syntax without the --force/-f flag at all. Uses plain \s (not
# "(^|\s)"): "^" can never legitimately fire here (a "+" can't be the first
# char of the whole command, since "git push" always precedes it) and this
# engine's "^" is buggy away from the very start of the pattern text (see the
# identity rule above and the main-push rule below).
if printf '%s' "$NORM" | grep -qE 'git[[:space:]]+push\b.*(--force|--force-with-lease|(^|\s)-f\b|\s\+)'; then
  block "force-push is banned for all sessions."
fi

# \b is the wrong boundary here: it treats "-" and "/" as boundaries too, so
# this used to block "main-feature", "feature-main", "feature/main-fix" —
# none of which push to main. The ref token must instead be whitespace- (or
# separator-) delimited. We pad the haystack with sentinel spaces and match
# only [[:space:]]/[;&|] — deliberately NOT "(^|...)" or "(...|$)" — because
# this grep's engine treats "^"/"$" used away from the very start/end of the
# pattern text as a no-op that always matches (reproduced: "x(^|q)y" matches
# "xy" on this box), which silently reopens the exact false-positive being
# fixed here.
# Colon-refspecs put the destination on the RIGHT of the colon
# ("git push origin fix-branch:main", "git push origin :main" — a delete of
# main — "git push origin HEAD:refs/heads/main"); the left side (source) is
# irrelevant to "does this land on main" and may be empty. [^[:space:]]*
# matches that left side (zero or more non-space chars, "" included) so
# these are caught alongside the pre-existing bare-token alternatives; the
# required [[:space:]] before the group and ([[:space:]]|[;&|]) after it
# still anchor the match to a whole ref token, so "feature:main-fix" (whose
# right side is "main-fix", not "main") is correctly left alone.
if [ "$MAIN_PUSH_GUARD_ACTIVE" = "1" ] \
   && printf ' %s ' "$NORM" | grep -qE 'git[[:space:]]+push\b[^;&|]*[[:space:]]([^[:space:]]*:refs/heads/main|[^[:space:]]*:main|refs/heads/main|main)([[:space:]]|[;&|])'; then
  block "pushing to main is reserved for the merge train. Push your feature branch and mark the PR ready; the train lands it after testing the combined tree."
fi

if printf '%s' "$NORM" | grep -qE 'rm -r?f?r?\b.*\.claude/worktrees'; then
  block "deleting worktree directories with rm orphans git metadata. Use 'git worktree remove <path>' — and only for your own worktree."
fi

exit 0
