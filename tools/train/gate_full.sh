#!/usr/bin/env bash
# Full tier: for branches touching optimizer math - the fast gate's
# exclusions are exactly where semantic conflicts live. Select per-branch
# via --gate; the train's cache keys on gate argv, so fast-approved trees
# never satisfy the full tier.
# cwd = the station clone holding the candidate merge commit.
# Tier 1 (always): ruff, fast pytest suite.
# Tier 2 (always): bit-exact characterization goldens - the machine-local
#   semantic tripwire no cloud CI can run.
# Tier 3 (full only): the analytic golden-master validation suite, plus the
#   optimizer fuzz/superadditivity/certificate proofs - the fast gate's own
#   `-m "not slow and not fuzz"` and golden_master_2025 exclusions.
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

$PY -m pytest tests/analytic -q --tb=short -m "" --ignore=tests/analytic/golden_master_2025
$PY -m pytest tests/test_optimizer_fuzz.py tests/test_optimizer_superadditivity.py \
  tests/test_optimality_certificates.py -q --tb=short -m ""
