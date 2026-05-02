#!/usr/bin/env bash
# Run the full ingestion test harness.
#
# * pytest    — unit tests in djangoscrap/tests/
# * selftest  — post-deploy canary (exits non-zero on regression)
#
# Exit code matches the first failing stage. Use this before calling any
# phase of INGESTION_IMPLEMENTATION_PLAN.md done.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PYTHON="${PYTHON:-python}"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
fi

echo "==> pytest"
"$PYTHON" -m pytest djangoscrap/tests/

echo
echo "==> manage.py ingestion_selftest"
"$PYTHON" manage.py ingestion_selftest

echo
echo "==> all stages green"
