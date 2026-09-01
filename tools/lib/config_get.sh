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
# Kept fast on the common (unconfigured) path: a config file is only ever
# looked for at the locations load_config() (or its trusted_only mode)
# itself resolves against, via cheap file-existence checks - the python
# helper is invoked ONLY when one of those actually exists. When none do,
# sy_cfg/sy_cfg_trusted are a pure bash string-echo with zero extra process
# cost, so every guard stays as fast as it was before this file existed.
#
# The python interpreter used to run that helper is
# "${SWITCHYARD_PYTHON:-python3}" - SWITCHYARD_PYTHON overrides a bare
# python3 wherever the ambient one predates tomllib (Python 3.11+; see
# switchyard_config.py) or isn't on PATH at all.
SY_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_sy_config_present() {
  [ -n "${SWITCHYARD_CONFIG:-}" ] && return 0
  local top
  top=$(git rev-parse --show-toplevel 2>/dev/null) || top=""
  [ -n "$top" ] && [ -f "$top/switchyard.toml" ] && return 0
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
  if _sy_config_present; then
    "${SWITCHYARD_PYTHON:-python3}" "$SY_LIB/switchyard_config_cli.py" "$key" "$default" \
      2>/dev/null || printf '%s\n' "$default"
  else
    printf '%s\n' "$default"
  fi
}

sy_cfg_trusted() {
  local key="$1" default="$2"
  if _sy_config_present_trusted; then
    "${SWITCHYARD_PYTHON:-python3}" "$SY_LIB/switchyard_config_cli.py" --trusted-only \
      "$key" "$default" 2>/dev/null || printf '%s\n' "$default"
  else
    printf '%s\n' "$default"
  fi
}
