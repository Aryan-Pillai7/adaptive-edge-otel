#!/usr/bin/env bash
# Run the test suites.
#
#   scripts/test.sh             unit tests, then integration tests
#   scripts/test.sh unit        unit only (fast, no Docker needed)
#   scripts/test.sh integration integration only (needs a running stack)
#
# The two suites are deliberately separate. Unit tests use in-memory exporters and
# must stay fast enough to run on every save; integration tests need the whole stack
# and take minutes. Merging them would make the fast feedback loop slow.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ensure_env
load_env

MODE="${1:-all}"

# Prefer the project venv if it exists, so `pip install -e app[dev]` is honoured
# without the caller having to activate anything.
PY="python"
for candidate in "$REPO_ROOT/.venv/Scripts/python.exe" "$REPO_ROOT/.venv/bin/python"; do
  [[ -x "$candidate" ]] && { PY="$candidate"; break; }
done
info "python: $PY"

failed=0

run_unit() {
  info "unit tests (app/tests — no stack required)"
  # rootdir resolves to app/, picking up app/pyproject.toml's pytest config.
  "$PY" -m pytest "$REPO_ROOT/app/tests" -q -p no:warnings || failed=1
}

run_integration() {
  info "integration tests (test/ — requires a running stack)"
  if ! curl -fsS "http://localhost:${COLLECTOR_HEALTH_PORT}/" >/dev/null 2>&1; then
    warn "stack is not running — start it with scripts/up.sh"
    warn "the suite will skip rather than fail"
  fi
  # Reports which arm-gated tests skipped and why, so a run that quietly skipped
  # everything cannot be mistaken for a run that passed everything.
  "$PY" -m pytest "$REPO_ROOT/test" -q -p no:warnings -rs || failed=1
}

case "$MODE" in
  unit)        run_unit ;;
  integration) run_integration ;;
  all)         run_unit; echo; run_integration ;;
  *)           die "unknown mode: $MODE (expected: unit | integration | all)" ;;
esac

echo
[[ $failed -eq 0 ]] || die "tests failed"
ok "all tests passed"
