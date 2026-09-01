#!/usr/bin/env bash
# Shared helper so bash guards can read one effective switchyard.toml value.
#
# Usage: source this file, then call  sy_cfg <key> <default>
#
# Guard-scoping keys - protected_branch, product_remote_match, as read by
# git_guard.sh's main-push ban - must call sy_cfg_trusted instead of sy_cfg.
# A repo-local switchyard.toml can ship in the very same PR/branch a guard is
# judging, so those two keys must never be resolvable from one: only
# $SWITCHYARD_CONFIG or ~/.config/switchyard/config.toml, both outside a PR
# author's control (see switchyard_config.load_config's trusted_only).
#
# Kept cheap on the common (unconfigured) path: a config file is only ever
# looked for at the locations load_config() (or its trusted_only mode)
# itself resolves against - the python helper is invoked ONLY when one of
# those actually exists. sy_cfg_trusted stays a pure bash string-echo with
# zero extra process cost when unconfigured.
#
# sy_cfg is no longer quite free in a git repo that carries no
# switchyard.toml in its working tree: it now also asks git whether the
# protected branch carries one, matching load_config's trunk fallback (see
# _sy_config_present). That is one `git cat-file -e` on a path that already
# ran `git rev-parse --show-toplevel`, and only when no working-tree file
# was found - a few milliseconds, never the python interpreter. Outside a
# git repo, and in any repo that does carry the file, nothing changed.
#
# The python interpreter used to run that helper is resolved via
# tools/lib/resolve_python.sh's sy_resolve_python - never a bare
# "${SWITCHYARD_PYTHON:-python3}": a host whose ambient python3 predates
# tomllib (Python 3.11+; e.g. macOS ships 3.9.6 at /usr/bin/python3) used to
# silently read every guard-scoping value as its hardcoded default, with the
# python-side warning thrown away by this file's own `2>/dev/null` - a
# SILENT protection gap for anyone with a custom protected_branch /
# product_remote_match. When a config file exists but sy_resolve_python
# finds nothing usable, both functions below now warn loudly on stderr
# (no longer swallowed) and fail safe to the caller's own default value -
# see sy_cfg_trusted's own comment for why that is enough for
# protected_branch but needs an extra check at the git_guard.sh call site
# for product_remote_match.
SY_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./resolve_python.sh
source "$SY_LIB/resolve_python.sh"

_sy_config_present() {
  [ -n "${SWITCHYARD_CONFIG:-}" ] && return 0
  local top
  top=$(git rev-parse --show-toplevel 2>/dev/null) || top=""
  if [ -n "$top" ]; then
    [ -f "$top/switchyard.toml" ] && return 0
    # Working tree has no copy, but the protected branch may - load_config
    # falls back to `git show <protected>:switchyard.toml` in exactly this
    # case. Without the same check here, bash would answer "unconfigured",
    # skip the parser entirely and hand every guard its hardcoded default
    # while the python side considered the repo fully configured. Two
    # readers of one config that disagree about whether it exists is the
    # bug this whole fallback was written to end, so they must agree.
    local protected
    protected=$(sy_cfg_trusted protected_branch "main")
    git cat-file -e "$protected:switchyard.toml" 2>/dev/null && return 0
  fi
  [ -f "${HOME:-/nonexistent}/.config/switchyard/config.toml" ] && return 0
  return 1
}

# Trusted-only presence check: deliberately does NOT look for a repo-local
# switchyard.toml at all (unlike _sy_config_present above) - mirrors
# load_config(trusted_only=True) exactly, so sy_cfg_trusted never even
# shells out to python over a repo-local file it would ignore anyway.
_sy_config_present_trusted() {
  [ -n "${SWITCHYARD_CONFIG:-}" ] && return 0
  [ -f "${HOME:-/nonexistent}/.config/switchyard/config.toml" ] && return 0
  return 1
}

sy_cfg() {
  local key="$1" default="$2"
  if ! _sy_config_present; then
    printf '%s\n' "$default"
    return 0
  fi
  local py
  py="$(sy_resolve_python)"
  if [ -z "$py" ]; then
    echo "switchyard: config present but unparseable (need python>=3.11); enforcing safe defaults" >&2
    printf '%s\n' "$default"
    return 1
  fi
  "$py" "$SY_LIB/switchyard_config_cli.py" "$key" "$default" || printf '%s\n' "$default"
}

sy_cfg_trusted() {
  local key="$1" default="$2"
  if ! _sy_config_present_trusted; then
    printf '%s\n' "$default"
    return 0
  fi
  local py
  py="$(sy_resolve_python)"
  if [ -z "$py" ]; then
    echo "switchyard: config present but unparseable (need python>=3.11); enforcing safe defaults" >&2
    # Generic fallback: return the caller's own default, unchanged. This is
    # enough on its own for protected_branch (every caller passes "main" as
    # its default, which is exactly the safe value to enforce here) but NOT
    # enough for product_remote_match (default ""), whose git_guard.sh call
    # site cannot tell "" apart from the unconfigured case just by value -
    # see git_guard.sh's own comment on that call for the extra check it
    # does using this same sy_resolve_python/_sy_config_present_trusted pair
    # to recover the distinction and fail safe the other way (enforce on
    # every origin) instead of silently under-protecting a custom config.
    printf '%s\n' "$default"
    return 1
  fi
  "$py" "$SY_LIB/switchyard_config_cli.py" --trusted-only "$key" "$default" \
    || printf '%s\n' "$default"
}
