#!/usr/bin/env bash
# PreToolUse guard for Bash commands in Claude Code sessions.
# Blocks git operations that are unsafe when many sessions share one repo:
#   - git stash (repo-global refs/stash silently destroys other sessions' work;
#     four independent incident reports; CLAUDE.md mandates WIP commits instead)
#   - git config user.* (identity drift breaks deploy author validation silently)
#   - force pushes
#   - any push to the protected branch (default "main") IN THE PRODUCT REPO
#     (that branch is written exclusively by the merge train there; other
#     repos are out of scope, see below) - including implicit destinations
#     (a bare push, or "push origin HEAD") resolved from the current branch
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
  #
  # Guard-scoping keys (product_remote_match, protected_branch) are read
  # TRUSTED-ONLY via sy_cfg_trusted: a repo-local switchyard.toml is
  # invisible to it by design (see tools/lib/config_get.sh /
  # switchyard_config.load_config's trusted_only). A PR that ships its own
  # repo-local switchyard.toml must never be able to retarget or disarm this
  # guard against its own push - only $SWITCHYARD_CONFIG or
  # ~/.config/switchyard/config.toml (both outside a PR author's control)
  # may override these two values.
  PRODUCT_MATCH=$(sy_cfg_trusted product_remote_match "")
  [ -z "$PRODUCT_MATCH" ] && PRODUCT_MATCH="06hp73/EV4SIM"
  case "$ORIGIN_URL" in
    *"$PRODUCT_MATCH"*) : ;;  # the product repo - stays enforced
    *) MAIN_PUSH_GUARD_ACTIVE=0 ;;
  esac
fi
PROTECTED_BRANCH=$(sy_cfg_trusted protected_branch "main")

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

# A pure regex over the whole command string (the previous approach) cannot
# express this rule correctly: a bare "git push" (no refspec at all) and
# "git push origin HEAD" both have an IMPLICIT destination - whatever the
# current branch's upstream is - which depends on which branch the command
# runs from, not on anything in the command text. Quoting a ref token
# ("main") also defeated a purely text-anchored match. So this rule instead
# tokenizes each push invocation and classifies its actual refspec
# argument(s), falling back to the current branch (resolved from the hook's
# own cwd) only when the destination is implicit. Blocks when ANY of:
#   - an explicit ref token's destination equals $PROTECTED_BRANCH (bare
#     name, "refs/heads/<branch>", or the right side of a "src:dst"
#     colon-refspec, including the delete form ":<branch>") - ALL quote
#     characters are stripped from the token first, wherever they sit in it
#     (not just a surrounding pair): a real shell removes them the same
#     way, so ma"in" and m"a"i"n" are both just "main" by the time git ever
#     sees them, and comparing the still-quoted literal against the bare
#     branch name would silently never match;
#   - there is NO explicit refspec at all (bare "git push", "git push
#     origin", "git push -u origin") and the current branch is
#     $PROTECTED_BRANCH, or the current branch could not be determined
#     (fail safe);
#   - the token is bare "HEAD" or "@" (no colon) - git treats "@" as a
#     literal synonym for HEAD - and the same current-branch check applies,
#     since both have an equally implicit destination;
#   - the invocation carries -C <path>, -c <key>=<value>, --git-dir[=]<path>,
#     or --work-tree[=]<path> BEFORE the push subcommand (any of which can
#     point the push at a repo other than the one at $CWD) together with
#     either an implicit destination (no explicit refspec, or a bare
#     HEAD/@) or an explicit one naming $PROTECTED_BRANCH. $CURRENT_BRANCH
#     below is always resolved from the HOOK's own cwd, which says nothing
#     about what a -C/--git-dir-redirected invocation actually has checked
#     out, so an implicit destination under one of these flags is refused
#     outright rather than evaluated against the wrong repo's branch - a
#     push that names an explicit, non-protected branch (e.g. "claude/x")
#     is unambiguous regardless of which repo -C points at and stays
#     allowed. This is deliberately narrow - exactly these four forms, not
#     a general git global-option parser - because they are the ones that
#     can change WHICH repo or identity a push acts against; an
#     unrecognized global flag (say "--no-pager") in front of "push" falls
#     back to the same not-classified-as-a-push gap this rule always had,
#     unchanged.
# Extracted as one "git ...push..." segment per invocation, starting at the
# "git" token itself (so any global options in front of "push" are visible
# to the tokenizer below) and running up to the next command separator
# (";", "&&", "||", "&", "|") or end of string - same segmentation the
# other rules in this file use for chained commands.
if [ "$MAIN_PUSH_GUARD_ACTIVE" = "1" ]; then
  CURRENT_BRANCH=""
  if [ -n "$CWD" ]; then
    CURRENT_BRANCH=$(git -C "$CWD" symbolic-ref --short HEAD 2>/dev/null) \
      || CURRENT_BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null) \
      || CURRENT_BRANCH=""
    # A detached HEAD reports the literal string "HEAD" from rev-parse's
    # fallback, not a real branch name - treat it as undeterminable.
    [ "$CURRENT_BRANCH" = "HEAD" ] && CURRENT_BRANCH=""
  fi

  # Process substitution (not a pipe): a pipe would run this while-loop in a
  # subshell in bash, and block()'s `exit 2` would then only exit that
  # subshell, silently letting the push through. `< <(...)` keeps the loop
  # in the current shell so exit actually aborts the whole script.
  while IFS= read -r GIT_SEG; do
    [ -z "$GIT_SEG" ] && continue
    # shellcheck disable=SC2206 # word-splitting is intentional and safe:
    # $GIT_SEG was isolated to a single git invocation above, so it
    # contains no ";"/"&"/"|" to mis-split on. TOKENS[0] is literally "git".
    TOKENS=($GIT_SEG)

    # Walk past global options that can appear before the subcommand and
    # change WHICH repo/identity this invocation targets. Anything else
    # (an unrecognized flag, or the subcommand itself) stops the walk.
    IDX=1
    HAS_REPO_ALTERING_OPT=0
    while [ "$IDX" -lt "${#TOKENS[@]}" ]; do
      TOK="${TOKENS[$IDX]}"
      case "$TOK" in
        -C|-c)
          HAS_REPO_ALTERING_OPT=1
          IDX=$((IDX + 2)) # skip the flag AND its separate-argument value
          ;;
        --git-dir=*|--work-tree=*)
          HAS_REPO_ALTERING_OPT=1
          IDX=$((IDX + 1))
          ;;
        --git-dir|--work-tree)
          HAS_REPO_ALTERING_OPT=1
          IDX=$((IDX + 2))
          ;;
        *)
          break
          ;;
      esac
    done

    [ "$IDX" -lt "${#TOKENS[@]}" ] || continue  # ran off the end - no subcommand at all
    [ "${TOKENS[$IDX]}" = "push" ] || continue  # subcommand isn't push - not our concern

    REST=("${TOKENS[@]:$((IDX + 1))}")

    POSITIONAL=()
    for TOK in "${REST[@]}"; do
      case "$TOK" in
        -*) : ;; # an option flag (-u, --force, --tags, ...), not a remote/refspec
        *) POSITIONAL+=("$TOK") ;;
      esac
    done

    BLOCK_THIS=0
    if [ "${#POSITIONAL[@]}" -le 1 ]; then
      # No explicit refspec: bare "git push" / "git push origin" /
      # "git push -u origin" - destination is implicit.
      if [ "$HAS_REPO_ALTERING_OPT" = "1" ]; then
        # -C/-c/--git-dir/--work-tree may target a wholly different repo -
        # $CURRENT_BRANCH speaks only for the hook's own cwd, so it cannot
        # answer for that repo. Fail safe: never trust it here.
        BLOCK_THIS=1
      elif [ -z "$CURRENT_BRANCH" ] || [ "$CURRENT_BRANCH" = "$PROTECTED_BRANCH" ]; then
        BLOCK_THIS=1
      fi
    else
      for REFSPEC in "${POSITIONAL[@]:1}"; do
        # Strip EVERY quote character wherever it sits, not just a matched
        # surrounding pair: a real shell does the same before git ever sees
        # the token, so ma"in" and m"a"i"n" both collapse to plain main.
        UNQ="${REFSPEC//\"/}"
        UNQ="${UNQ//\'/}"

        DST="$UNQ"
        case "$UNQ" in
          *:*) DST="${UNQ#*:}" ;; # colon refspec: only the destination (right side) matters
        esac

        if [ "$DST" = "$PROTECTED_BRANCH" ] || [ "$DST" = "refs/heads/$PROTECTED_BRANCH" ]; then
          BLOCK_THIS=1
        elif { [ "$UNQ" = "HEAD" ] || [ "$UNQ" = "@" ]; }; then
          if [ "$HAS_REPO_ALTERING_OPT" = "1" ]; then
            # HEAD/@ resolves against THAT invocation's own repo under -C/
            # -c/--git-dir/--work-tree, which $CURRENT_BRANCH cannot speak
            # to either - same fail-safe reasoning as the refspec-less case.
            BLOCK_THIS=1
          elif [ -z "$CURRENT_BRANCH" ] || [ "$CURRENT_BRANCH" = "$PROTECTED_BRANCH" ]; then
            BLOCK_THIS=1
          fi
        fi
      done
    fi

    if [ "$BLOCK_THIS" = "1" ]; then
      block "pushing to $PROTECTED_BRANCH is reserved for the merge train. Push your feature branch and mark the PR ready; the train lands it after testing the combined tree."
    fi
  done < <(printf '%s' "$NORM" | grep -oE 'git\b[^;&|]*')
fi

# .claude/worktrees is always checked, regardless of config - it is the
# convention this repo's own tooling assumes. worktree_dir (switchyard.toml,
# used by `switchyard track new`/`done`) additionally names wherever THIS
# project's track worktrees actually live, when that differs; it is
# advisory, not guard-scoping (see sy_cfg_trusted's docstring) - the worst a
# hostile repo-local override could do here is fail to add protection for a
# custom path, never remove the always-on .claude/worktrees protection
# above. Fixed-string (grep -F), not a regex: a filesystem path is data, not
# a pattern, and may itself contain regex metacharacters.
if printf '%s' "$NORM" | grep -qE 'rm -r?f?r?\b'; then
  if printf '%s' "$NORM" | grep -qF '.claude/worktrees'; then
    block "deleting worktree directories with rm orphans git metadata. Use 'git worktree remove <path>' — and only for your own worktree."
  fi
  WORKTREE_DIR=$(sy_cfg worktree_dir "")
  if [ -n "$WORKTREE_DIR" ] && printf '%s' "$NORM" | grep -qF "$WORKTREE_DIR"; then
    block "deleting worktree directories with rm orphans git metadata. Use 'git worktree remove <path>' — and only for your own worktree."
  fi
fi

exit 0
