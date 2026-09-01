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
  # sy_cfg_trusted returns "" both when no trusted config exists at all (the
  # common case - fall back to the hardcoded EV4SIM default below, unchanged
  # from before this check existed) AND when a trusted config file DOES
  # exist but no Python >=3.11 was available to read it (see
  # config_get.sh's sy_cfg_trusted) - those two cases must NOT be treated
  # the same way. Silently falling back to the EV4SIM default in the second
  # case would be a real protection gap: someone who configured a custom
  # product_remote_match for their OWN product repo would have that setting
  # invisibly dropped, the guard would compare their repo's origin against
  # "06hp73/EV4SIM" instead, find no match, and disable itself for the very
  # repo it was configured to protect - silently. Recompute the same
  # "config present but unparseable" condition sy_cfg_trusted already
  # checked internally (both helpers are cheap/pure-bash-first, see
  # config_get.sh) to tell the two apart, and fail safe the OTHER way in the
  # unparseable case: enforce on every origin, matching the "empty means
  # always enforce" fallback tools/guards/pre_push_hook.sh's is_product_remote
  # already applies to this identical empty value - never disable the ban
  # just because it could not be told what to scope itself to.
  if [ -z "$PRODUCT_MATCH" ] && _sy_config_present_trusted && [ -z "$(sy_resolve_python)" ]; then
    : # unparseable trusted config - stay enforced on every origin (no-op)
  else
    [ -z "$PRODUCT_MATCH" ] && PRODUCT_MATCH="06hp73/EV4SIM"
    case "$ORIGIN_URL" in
      *"$PRODUCT_MATCH"*) : ;;  # the product repo - stays enforced
      *) MAIN_PUSH_GUARD_ACTIVE=0 ;;
    esac
  fi
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
#     name, "heads/<branch>" or "refs/heads/<branch>", or the right side of
#     a "src:dst" colon-refspec, including the delete form ":<branch>") -
#     ALL quote AND backslash characters are stripped from the token first,
#     wherever they sit in it (not just a surrounding pair, and not just at
#     the edges): a real shell removes them the same way, so ma"in",
#     m"a"i"n", and \main or ma\in are all just "main" by the time git ever
#     sees them, and comparing the still-quoted-or-escaped literal against
#     the bare branch name would silently never match;
#   - there is NO explicit refspec at all (bare "git push", "git push
#     origin", "git push -u origin") and the current branch is
#     $PROTECTED_BRANCH, or the current branch could not be determined
#     (fail safe);
#   - the token is bare "HEAD" or "@" (no colon) - git treats "@" as a
#     literal synonym for HEAD - and the same current-branch check applies,
#     since both have an equally implicit destination;
#   - the invocation carries -C <path>, -c <key>=<value>, --git-dir[=]<path>,
#     --work-tree[=]<path>, OR a leading GIT_DIR=/GIT_WORK_TREE= environment
#     assignment BEFORE the push subcommand (any of which can point the push
#     at a repo other than the one at $CWD) together with either an implicit
#     destination (no explicit refspec, or a bare HEAD/@) or an explicit one
#     naming $PROTECTED_BRANCH. $CURRENT_BRANCH below is always resolved
#     from the HOOK's own cwd, which says nothing about what a redirected
#     invocation actually has checked out, so an implicit destination under
#     one of these is refused outright rather than evaluated against the
#     wrong repo's branch - a push that names an explicit, non-protected
#     branch (e.g. "claude/x") is unambiguous regardless of which repo is
#     actually targeted and stays allowed. Every OTHER leading "-"-prefixed
#     global option (--no-pager, -p/-P, an unrecognized future flag, ...) is
#     generically skipped by the tokenizer below rather than stopping
#     classification cold - this is not a general git global-option parser
#     (an obscure flag that itself takes a separate argument can still
#     misalign the walk), just wide enough that the common no-argument ones
#     cannot silently dodge classification the way they used to.
# Extracted as one "git ...push..." segment per invocation, starting at a
# "git" token that begins a shell word (preceded by whitespace, a command
# separator, or start-of-string - NOT merely by a word-boundary, which a
# path ending in ".git" - the ordinary shape of $GIT_DIR - would also
# satisfy and so falsely self-match as if IT were the git invocation) so
# any global options in front of "push" are visible to the tokenizer below,
# and running up to the next command separator (";", "&&", "||", "&", "|")
# or end of string - same segmentation the other rules in this file use for
# chained commands.
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

  # GIT_DIR=/GIT_WORK_TREE= as an environment ASSIGNMENT sitting before the
  # word "git" redirects which repo a plain invocation acts on exactly like
  # -C/--git-dir/--work-tree do as flags - but because it sits before "git"
  # rather than after it, the per-invocation extraction below (anchored at
  # "git" itself) never sees it as part of the same segment, so it needs its
  # own, separate detection. Checked once for the whole command rather than
  # re-derived per matched segment: a coarse over-approximation whose only
  # possible error is treating an unrelated implicit-destination push
  # elsewhere in the same compound command as ALSO redirected - which only
  # ever makes that push fail-safe-block, never the reverse (an explicit,
  # non-protected-named push is unaffected either way - see the
  # HAS_REPO_ALTERING_OPT reasoning above).
  ENV_REPO_REDIRECT=0
  if printf '%s' "$NORM" | grep -qE '(^|[;&|]\s*|&&\s*)\s*(GIT_DIR|GIT_WORK_TREE)=\S*\s+git\b'; then
    ENV_REPO_REDIRECT=1
  fi

  # Process substitution (not a pipe): a pipe would run this while-loop in a
  # subshell in bash, and block()'s `exit 2` would then only exit that
  # subshell, silently letting the push through. `< <(...)` keeps the loop
  # in the current shell so exit actually aborts the whole script.
  while IFS= read -r GIT_SEG; do
    [ -z "$GIT_SEG" ] && continue
    # shellcheck disable=SC2206 # word-splitting is intentional and safe:
    # $GIT_SEG was isolated to a single git invocation above, so it
    # contains no ";"/"&"/"|" to mis-split on internally - though the
    # extraction's left boundary can itself be a bare "&"/";" glued onto the
    # front with no space (e.g. "cd /x&&git push origin main" segments as
    # "&git push origin main"), so TOKENS[0] is not always literally "git"
    # any more. That is harmless: nothing below ever inspects TOKENS[0]
    # itself, the walk always starts at index 1.
    TOKENS=($GIT_SEG)

    # Walk past global options that can appear before the subcommand and
    # change WHICH repo/identity this invocation targets. -C/-c/--git-dir/
    # --work-tree are singled out because each can redirect which repo or
    # identity is acted on, which forces the implicit-destination fail-safe
    # below; every OTHER "-"-prefixed token (--no-pager, -p/-P, an
    # unrecognized future flag, ...) is skipped generically, one token at a
    # time, since it does not - only the subcommand itself (or running off
    # the end of the tokens) stops the walk now.
    IDX=1
    HAS_REPO_ALTERING_OPT=$ENV_REPO_REDIRECT
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
        -*)
          # Some other global option. Assumed to take no separate-argument
          # value (true of the common ones this is aimed at - --no-pager,
          # -p/-P, --no-replace-objects, an unrecognized future flag): skip
          # just this one token and keep walking, rather than stopping the
          # walk cold the way an unrecognized flag used to - which left the
          # WHOLE invocation unclassified (neither push nor anything else)
          # and so silently allowed, regardless of what followed it. An
          # obscure global flag that DOES take a separate argument can still
          # misalign this walk; that residual gap is exactly what the
          # pre-push hook (tools/guards/pre_push_hook.sh) exists to catch
          # regardless, since it never depends on this text parse at all.
          IDX=$((IDX + 1))
          ;;
        *)
          break
          ;;
      esac
    done

    [ "$IDX" -lt "${#TOKENS[@]}" ] || continue  # ran off the end - no subcommand at all

    # Dequote before comparing: git "push" / git pu"sh" both collapse to the
    # word push once a real shell strips the quote characters, but the raw
    # token still carries them here - the same reasoning the refspec
    # stripping below already applies, just for the subcommand word itself.
    SUBCMD="${TOKENS[$IDX]}"
    SUBCMD="${SUBCMD//\"/}"
    SUBCMD="${SUBCMD//\'/}"
    [ "$SUBCMD" = "push" ] || continue  # subcommand isn't push - not our concern

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
        # Strip EVERY quote AND backslash character wherever it sits, not
        # just a matched surrounding pair or a leading escape: a real shell
        # does the same before git ever sees the token, so ma"in", m"a"i"n",
        # \main, and ma\in all collapse to plain main.
        UNQ="${REFSPEC//\"/}"
        UNQ="${UNQ//\'/}"
        UNQ="${UNQ//\\/}"

        DST="$UNQ"
        case "$UNQ" in
          *:*) DST="${UNQ#*:}" ;; # colon refspec: only the destination (right side) matters
        esac
        # git also resolves a partial ref path missing the "refs/" prefix -
        # "heads/main" means exactly refs/heads/main, same as the full form
        # already checked below - so normalize that one extra shape too.
        # Safe to do unconditionally: "refs/heads/main" itself starts with
        # "refs/", never "heads/", so this can never touch that other case.
        case "$DST" in
          heads/*) DST="${DST#heads/}" ;;
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
  done < <(printf '%s' "$NORM" | grep -oE '(^|[;&|[:space:]])git\b[^;&|]*')
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
