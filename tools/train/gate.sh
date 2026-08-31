#!/usr/bin/env bash
# Train gate: what must be green before a merge result may reach main.
# cwd = the station clone holding the candidate merge commit.
# Tier 1 (always): ruff, fast pytest suite.
# Tier 2 (always): bit-exact characterization goldens - the machine-local
#   semantic tripwire no cloud CI can run.
# Import preflight pins the tested tree to THIS checkout: PYTHONPATH=src wins
# over the main checkout's editable install, and we verify it.
set -eu
PY=/Users/storslasken/Developer/EV4XL-SIM/.venv/bin/python
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
