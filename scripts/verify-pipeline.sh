#!/usr/bin/env bash
# END-TO-END PIPELINE GATE.
#
# Emits a uniquely tagged burst of all three signals at the Collector, then proves
# each one arrived in its own backend. This is the check that Phase 2 exists to pass,
# and the basis of the Phase 5 integration tests.
#
# It asserts on DATA, not on process health. A green smoke.sh only means the
# containers are alive; this is what distinguishes "running" from "working".

# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker; need curl
ensure_env
load_env

RUN_ID="verify-$(date +%s)"
export TELEMETRYGEN_RUN_ID="$RUN_ID"
export TELEMETRYGEN_SERVICE="${TELEMETRYGEN_SERVICE:-telemetrygen-probe}"
export TELEMETRYGEN_COUNT="${TELEMETRYGEN_COUNT:-20}"

failed=0

info "emitting all three signals  (run_id=$RUN_ID)"
bash "$REPO_ROOT/scripts/telemetrygen.sh" >/dev/null || die "telemetrygen failed"
ok "emitted"

echo
info "waiting for data to land"
# Data is not instantly visible: the Collector batches (5s), Loki flushes chunks, and
# Tempo has to make the trace searchable. So each assertion retries rather than
# sleeping a guessed amount and hoping.
# NOTE: callers must append `|| true`. lib.sh sets `set -e`, so a bare failing call
# would abort the script at the first missing signal -- you would fix one backend,
# re-run, and discover the next. Collecting all failures in one pass is the point.
assert_eventually() {
  local name="$1" cmd="$2" tries="${3:-20}"
  local i out
  for (( i = 1; i <= tries; i++ )); do
    if out="$(eval "$cmd" 2>/dev/null)" && [[ -n "$out" && "$out" != "0" ]]; then
      ok "$name  ($out)"
      return 0
    fi
    sleep 2
  done
  warn "$name — NOT FOUND after $((tries * 2))s"
  failed=1
  return 1
}

# --- metrics --------------------------------------------------------------
# /api/v1/series, NOT the instant /api/v1/query: VM applies -search.latencyOffset
# (30s) to instant queries, so a sample that landed correctly still reads back empty.
assert_eventually "metrics in VictoriaMetrics" \
  "curl -s -G 'http://localhost:${VICTORIAMETRICS_PORT}/api/v1/series' \
     --data-urlencode 'match[]={run_id=\"$RUN_ID\"}' \
   | python -c \"import sys,json;print(len(json.load(sys.stdin)['data']))\"" || true

# --- logs -----------------------------------------------------------------
assert_eventually "logs in Loki" \
  "curl -s -G 'http://localhost:${LOKI_PORT}/loki/api/v1/query_range' \
     --data-urlencode 'query={service_name=\"$TELEMETRYGEN_SERVICE\"} | run_id=\`$RUN_ID\`' \
   | python -c \"import sys,json;d=json.load(sys.stdin)['data']['result'];print(sum(len(s['values']) for s in d))\"" || true

# --- traces ---------------------------------------------------------------
assert_eventually "traces in Tempo" \
  "curl -s -G 'http://localhost:${TEMPO_QUERY_PORT}/api/search' \
     --data-urlencode 'q={ resource.run_id = \"$RUN_ID\" }' --data-urlencode 'limit=20' \
   | python -c \"import sys,json;print(len(json.load(sys.stdin).get('traces') or []))\"" || true

# --- the measurement instrument -------------------------------------------
# Phase 4's entire before/after claim is read from these counters, so verify now that
# they exist and are moving. Discovering they are absent while measuring would be a
# much more expensive surprise.
echo
info "collector self-telemetry"
for sig in spans metric_points log_records; do
  n="$(curl -s "http://localhost:${COLLECTOR_METRICS_PORT}/metrics" \
       | grep -E "^otelcol_receiver_accepted_${sig}(_total)?\{" | head -1 | awk '{print $NF}')"
  if [[ -n "$n" && "$n" != "0" ]]; then
    ok "receiver accepted ${sig}: $n"
  else
    warn "no otelcol_receiver_accepted_${sig} counter — Phase 4 measurement depends on this"
    failed=1
  fi
done

echo
if [[ $failed -eq 0 ]]; then
  ok "PIPELINE VERIFIED — all three signals reached their backends (run_id=$RUN_ID)"
else
  die "pipeline verification FAILED (run_id=$RUN_ID) — try: scripts/logs.sh collector"
fi
