#!/usr/bin/env bash
# Train gate: what must be green before a merge result may reach main.
# cwd = the station clone holding the candidate merge commit.
# Tier 1 (always): ruff, fast pytest suite.
# Tier 2 (always): bit-exact characterization goldens - the machine-local
#   semantic tripwire no cloud CI can run.
# Import preflight pins the tested tree to THIS checkout: PYTHONPATH=src wins
# over the main checkout's editable install, and we verify it.
#
# Interpreter: SWITCHYARD_PYTHON (env var) wins if set, else switchyard.toml's
# `python` key (via sy_cfg), else a bare "python3" - adapt cfg.python (or
# export SWITCHYARD_PYTHON) if your project needs a specific interpreter,
# e.g. a venv that isn't on PATH.
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config_get.sh
source "$SCRIPT_DIR/../lib/config_get.sh"
PY="${SWITCHYARD_PYTHON:-$(sy_cfg python python3)}"
export PYTHONPATH="$PWD/src"
export EV4XL_SKIP_DASH_BOOTSTRAP=1

$PY - <<'EOF'
import os, sys
import optimizer_core
expected = os.path.join(os.getcwd(), "src")
actual = os.path.dirname(os.path.dirname(optimizer_core.__file__))
if os.path.realpath(actual) != os.path.realpath(expected):
    print(f"gate preflight FAILED: importing from {actual}, expected {expected}", file=sys.stderr)
    sys.exit(1)
EOF

$PY -m ruff check src tests tools
$PY -m pytest tests/ -q --tb=short -m "not slow and not fuzz" \
  --ignore=tests/browser \
  --ignore=tests/analytic/golden_master_2025
$PY -m pytest tests/analytic/test_characterization_snapshots.py -q --tb=short -m ""
