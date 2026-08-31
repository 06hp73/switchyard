#!/usr/bin/env bash
# Shared helper so bash guards can read one effective switchyard.toml value.
#
# Usage: source this file, then call  sy_cfg <key> <default>
#
# Kept fast on the common (unconfigured) path: a config file is only ever
# looked for at the three locations load_config() itself resolves against
# (env var, repo-toplevel switchyard.toml, ~/.config/switchyard/config.toml)
# via cheap file-existence checks - the python helper is invoked ONLY when
# one of those actually exists. When none do, sy_cfg is a pure bash
# string-echo with zero extra process cost, so every guard stays as fast as
# it was before this file existed.
SY_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_sy_config_present() {
  [ -n "${SWITCHYARD_CONFIG:-}" ] && return 0
  local top
  top=$(git rev-parse --show-toplevel 2>/dev/null) || top=""
  [ -n "$top" ] && [ -f "$top/switchyard.toml" ] && return 0
  [ -f "${HOME:-/nonexistent}/.config/switchyard/config.toml" ] && return 0
  return 1
}

sy_cfg() {
  local key="$1" default="$2"
  if _sy_config_present; then
    python3 "$SY_LIB/switchyard_config_cli.py" "$key" "$default" 2>/dev/null \
      || printf '%s\n' "$default"
  else
    printf '%s\n' "$default"
  fi
}
