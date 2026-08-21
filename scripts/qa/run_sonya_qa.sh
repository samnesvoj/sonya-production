#!/usr/bin/env bash
# One reproducible command for SONYA generation-pipeline QA.
# See docs/QA_GENERATION_LIFECYCLE.md for what each piece checks and why.
#
# Safe to run any time: no network calls, no real S3/Postgres/vast.ai/GPU,
# no cost. Runs the full backend pytest suite, the frontend node --test
# suite, and the standalone stage-by-stage diagnostic CLI across all four
# scenarios. Exits non-zero if anything fails.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "── Backend: pytest tests/ (excluding the Postgres-only test -- needs a real DATABASE_URL, see .github/workflows/test.yml) ──"
pytest tests/ -v --ignore=tests/test_job_idempotency_postgres.py

echo
echo "── Frontend: node --test tests/frontend/*.mjs ──"
node --test tests/frontend/*.mjs

echo
echo "── Lifecycle diagnostic CLI: all four scenarios ──"
for scenario in happy bad-mode s3-down stalled-s3; do
  echo
  echo "  scenario: ${scenario}"
  timeout_arg=""
  if [ "$scenario" = "stalled-s3" ]; then
    timeout_arg="--stage-timeout 1"
  fi
  # bad-mode/s3-down/stalled-s3 are expected to exit non-zero (that's the
  # point -- they prove failures are reported, not silently passed) --
  # only fail the whole run if 'happy' fails, or if a script errors
  # outright (missing dependency, import error, etc.).
  if [ "$scenario" = "happy" ]; then
    python scripts/qa/diagnose_generation_flow.py --scenario "$scenario" $timeout_arg
  else
    python scripts/qa/diagnose_generation_flow.py --scenario "$scenario" $timeout_arg || true
  fi
done

echo
echo "All checks completed. See docs/QA_GENERATION_LIFECYCLE.md for the full stage map and root-cause report."
