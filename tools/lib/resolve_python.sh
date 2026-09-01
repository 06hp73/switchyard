#!/usr/bin/env bash
# Shared Python interpreter resolver: finds an interpreter guaranteed to have
# tomllib (stdlib since Python 3.11) so switchyard.toml can actually be
# parsed. Used by BOTH bin/switchyard (running the CLI itself) and
# config_get.sh's sy_cfg/sy_cfg_trusted (reading one config value for a bash
# guard) - a single place to fix or extend interpreter selection instead of
# two copies that can drift apart.
#
# Background: `${SWITCHYARD_PYTHON:-python3}`, used verbatim everywhere
# before this file existed, silently breaks on any host whose bare `python3`
# predates 3.11 (e.g. macOS ships 3.9.6 at /usr/bin/python3) - `import
# tomllib` fails, switchyard_config.py catches that and falls back to
# defaults, and nothing about that fallback is loud enough to notice. This
# resolver exists so callers can find a REAL >=3.11 interpreter first and
# only fail (loudly, at the call site - see bin/switchyard and
# config_get.sh) when none exists anywhere.
#
# Usage: source this file, then call  sy_resolve_python
#   Prints the chosen interpreter's command name/path on stdout and returns
#   0, or prints nothing and returns 1 if no usable interpreter was found.
#   Check the empty-output case (or the return code) rather than assume
#   success - see bin/switchyard (hard exit) and config_get.sh (fail-safe
#   fallback) for the two ways callers act on "not found".
#
# Resolution order, first usable one wins:
#   1. $SWITCHYARD_PYTHON, if set - the explicit override/escape hatch for a
#      host where every candidate below is unusable (e.g. a >=3.11
#      interpreter installed under a name this resolver does not guess).
#      Still version-checked (see step 3's reasoning) rather than trusted
#      blindly: a broken override must never silently pass as "found" only
#      for the caller to discover tomllib is missing two layers down. If it
#      fails the check, resolution falls through to auto-detection below
#      rather than giving up immediately - an explicit-but-wrong override
#      should never leave a caller worse off than not setting it at all.
#   2. python3.13, python3.12, python3.11 - the first one found on PATH,
#      trusted by its versioned name alone: a binary actually named
#      python3.11 IS Python 3.11 by construction, so no extra runtime check
#      is needed (or meaningfully possible without invoking the very
#      interpreter being verified).
#   3. bare `python3`, but ONLY if actually running it to check
#      `sys.version_info >= (3, 11)` succeeds - the "ambient python3 already
#      happens to be new enough" case (most Linux distros in 2025+), which
#      must be verified rather than assumed since an ambient python3 can be
#      anything.
# Nothing usable: prints nothing, returns 1.
sy_resolve_python() {
  if [ -n "${SWITCHYARD_PYTHON:-}" ] \
    && "$SWITCHYARD_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1; then
    printf '%s\n' "$SWITCHYARD_PYTHON"
    return 0
  fi

  local candidate
  for candidate in python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  if command -v python3 >/dev/null 2>&1 \
    && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return 0
  fi

  return 1
}
