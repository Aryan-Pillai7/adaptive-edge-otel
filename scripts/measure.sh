#!/usr/bin/env bash
# Measure what the pipeline does to a flood.
#
# Runs the SAME flood against whichever Collector config is loaded and records what
# came out the other side. Run it once per arm:
#
#   COLLECTOR_CONFIG=collector.passthrough.yaml  -> the "before" baseline
#   COLLECTOR_CONFIG=collector.yaml              -> the "after" result (Phase 4)
#
# The comparison is only valid if both arms see an identical flood, so the flood
# profile is read from .env, echoed back by the app, and recorded verbatim in the
# result file. If the profile changes, previously recorded numbers no longer compare
# and must be re-taken -- which is why the profile is stored WITH the numbers rather
# than assumed.
#
# Results: .measure/<arm>-<timestamp>.json  (gitignored; the numbers that matter are
# promoted into decisions.md by hand so they cannot drift silently.)

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker; need curl; need python
ensure_env
load_env

OUT_DIR="$REPO_ROOT/.measure"
mkdir -p "$OUT_DIR"

# Reset to a clean stack before measuring, unless told not to.
#
# This is NOT optional hygiene, it is required for the arms to be comparable. The SDK
# uses CUMULATIVE temporality, so every export cycle re-sends every series the process
# has ever created. A second flood in the same process therefore inherits the first
# one's 6,000 series and re-exports them every 10s -- measured back to back, run 2
# reported 120,949 metric points against run 1's 34,253 for an identical flood.
#
# That is also the single most important thing this project demonstrates: unbounded
# cardinality does not just cost storage, it multiplies EXPORT VOLUME on every
# interval for the entire life of the process.
RESET=1
[[ "${1:-}" == "--no-reset" ]] && RESET=0

# Time to let everything in flight actually land before the second snapshot. Must
# exceed the slowest buffer in the chain: the Collector's batch timeout (5s), the
# app's metric export interval (10s), and Loki's chunk flush. Snapshotting too early
# would credit the pipeline with a reduction that is really just data still in a queue.
DRAIN_SECONDS="${MEASURE_DRAIN_SECONDS:-35}"

# --- collectors -----------------------------------------------------------
collector_counter() {
  # Sums every label combination for one counter (per-exporter, per-receiver series).
  curl -s "http://localhost:${COLLECTOR_METRICS_PORT}/metrics" \
    | awk -v name="$1" '$0 !~ /^#/ && index($0, name"{") == 1 { s += $NF } END { printf "%d", s+0 }'
}

vm_total_series() {
  curl -s "http://localhost:${VICTORIAMETRICS_PORT}/api/v1/status/tsdb" \
    | python -c "import sys,json;d=json.load(sys.stdin);print((d.get('data') or d).get('totalSeries',0))" 2>/dev/null || echo 0
}

vm_series_matching() {
  # /api/v1/series, never the instant query: VM applies -search.latencyOffset to
  # instant queries, so freshly written samples read back as nothing at all.
  curl -s -G "http://localhost:${VICTORIAMETRICS_PORT}/api/v1/series" \
    --data-urlencode "match[]=$1" \
    | python -c "import sys,json;print(len(json.load(sys.stdin).get('data') or []))" 2>/dev/null || echo 0
}

loki_lines() {
  # Counts log lines over a window rather than diffing a counter, because Loki has no
  # equivalent cumulative ingest counter exposed per stream.
  local window="$1"
  curl -s -G "http://localhost:${LOKI_PORT}/loki/api/v1/query" \
    --data-urlencode "query=sum(count_over_time({service_name=\"${OTEL_SERVICE_NAME}\"}[${window}]))" \
    | python -c "
import sys, json
try:
    r = json.load(sys.stdin)['data']['result']
    print(int(float(r[0]['value'][1])) if r else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0
}

container_mem_mib() {
  docker stats --no-stream --format '{{.MemUsage}}' "$1" 2>/dev/null \
    | awk -F' / ' '{ v=$1; sub(/[A-Za-z]+$/,"",v);
                     if ($1 ~ /GiB/) v*=1024; else if ($1 ~ /KiB/) v/=1024;
                     printf "%d", v }'
}

# --- run ------------------------------------------------------------------
if (( RESET )); then
  info "resetting stack for a clean measurement (use --no-reset to skip)"
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  compose up -d --wait >/dev/null 2>&1 || die "stack failed to come back up"
  ok "clean stack up"
fi

# Which arm are we measuring? Read from the RUNNING container rather than from a flag,
# so the label can never disagree with the config that actually processed the data.
# Resolved after any reset, since the container is recreated by it.
ARM="$(docker inspect aeo-collector --format '{{range .Config.Cmd}}{{.}} {{end}}' 2>/dev/null        | sed -E 's|.*/etc/otelcol/||; s|\.yaml.*||')"
[[ -n "$ARM" ]] || die "collector is not running — start it with scripts/up.sh"

info "arm: $ARM"
info "flood profile: ${FLOOD_DURATION_SECONDS}s x ${FLOOD_LOGS_PER_SECOND} logs/s + ${FLOOD_UNIQUE_LABELS_PER_SECOND} unique labels/s"

curl -fsS "http://localhost:${APP_PORT}/health" >/dev/null 2>&1 \
  || die "app is not responding on :${APP_PORT} — start it with scripts/up.sh"

info "snapshot: before"
BEFORE_ACC_SPANS=$(collector_counter otelcol_receiver_accepted_spans)
BEFORE_ACC_POINTS=$(collector_counter otelcol_receiver_accepted_metric_points)
BEFORE_ACC_LOGS=$(collector_counter otelcol_receiver_accepted_log_records)
BEFORE_SENT_SPANS=$(collector_counter otelcol_exporter_sent_spans)
BEFORE_SENT_POINTS=$(collector_counter otelcol_exporter_sent_metric_points)
BEFORE_SENT_LOGS=$(collector_counter otelcol_exporter_sent_log_records)
BEFORE_VM_SERIES=$(vm_total_series)
# Snapshotted before AND after. /api/v1/series returns every series ever created in
# the retention window, so the raw count is cumulative -- reporting it directly would
# credit a later arm with series an earlier arm created. Only the delta is per-run.
BEFORE_BOMB_SERIES=$(vm_series_matching '{__name__="edgeapp_db_timeouts_total"}')

# Steady-state traffic runs FOR THE WHOLE measurement window, flood included. The
# flood emits only logs and metric points; without concurrent requests there would be
# no spans to sample, so the trace column would measure nothing at all.
info "starting steady-state traffic in the background"
bash "$REPO_ROOT/scripts/load.sh" "$(( FLOOD_DURATION_SECONDS + 5 ))" "${MEASURE_RPS:-10}"   >/dev/null 2>&1 &
LOAD_PID=$!

info "triggering flood"
FLOOD_JSON="$(curl -fsS -X POST "http://localhost:${APP_PORT}/simulate/flood" \
  -H 'Content-Type: application/json' -d '{}')" || die "could not start flood"

FLOOD_ID="$(python -c "import sys,json;print(json.loads(sys.argv[1])['flood_id'])" "$FLOOD_JSON")"
EXPECTED_LOGS="$(python -c "import sys,json;print(json.loads(sys.argv[1])['profile']['total_log_records'])" "$FLOOD_JSON")"
EXPECTED_LABELS="$(python -c "import sys,json;print(json.loads(sys.argv[1])['profile']['total_unique_labels'])" "$FLOOD_JSON")"
ok "flood $FLOOD_ID started — expecting $EXPECTED_LOGS log records, $EXPECTED_LABELS unique label values"

info "waiting for the flood to finish"
while true; do
  running="$(curl -fsS "http://localhost:${APP_PORT}/simulate/flood/status" \
    | python -c "import sys,json;print(json.load(sys.stdin).get('running'))" 2>/dev/null || echo False)"
  [[ "$running" == "True" ]] || break
  sleep 2
done
ok "flood finished"

# Traffic is sized to outlast the flood slightly; make sure it is done before the
# drain begins, or its final spans would land after the "after" snapshot.
wait "$LOAD_PID" 2>/dev/null || true
ok "steady-state traffic finished"

info "draining for ${DRAIN_SECONDS}s (batch timeout + metric export interval + loki flush)"
sleep "$DRAIN_SECONDS"

info "snapshot: after"
AFTER_ACC_SPANS=$(collector_counter otelcol_receiver_accepted_spans)
AFTER_ACC_POINTS=$(collector_counter otelcol_receiver_accepted_metric_points)
AFTER_ACC_LOGS=$(collector_counter otelcol_receiver_accepted_log_records)
AFTER_SENT_SPANS=$(collector_counter otelcol_exporter_sent_spans)
AFTER_SENT_POINTS=$(collector_counter otelcol_exporter_sent_metric_points)
AFTER_SENT_LOGS=$(collector_counter otelcol_exporter_sent_log_records)
AFTER_VM_SERIES=$(vm_total_series)

WINDOW="$(( FLOOD_DURATION_SECONDS + DRAIN_SECONDS + 60 ))s"
LOKI_LINES=$(loki_lines "$WINDOW")
AFTER_BOMB_SERIES=$(vm_series_matching '{__name__="edgeapp_db_timeouts_total"}')

MEM_COLLECTOR=$(container_mem_mib aeo-collector)
MEM_VM=$(container_mem_mib aeo-victoriametrics)
MEM_LOKI=$(container_mem_mib aeo-loki)
MEM_TEMPO=$(container_mem_mib aeo-tempo)

STAMP="$(date +%Y%m%d-%H%M%S)"
RESULT="$OUT_DIR/${ARM}-${STAMP}.json"

python - "$RESULT" <<PYEOF
import json, sys

arm = "$ARM"
d = {
    "arm": arm,
    "timestamp": "$STAMP",
    "flood_id": "$FLOOD_ID",
    "collector_config": "$COLLECTOR_CONFIG",
    "profile": {
        "duration_seconds": $FLOOD_DURATION_SECONDS,
        "logs_per_second": $FLOOD_LOGS_PER_SECOND,
        "unique_labels_per_second": $FLOOD_UNIQUE_LABELS_PER_SECOND,
        "expected_log_records": $EXPECTED_LOGS,
        "expected_unique_labels": $EXPECTED_LABELS,
    },
    "collector": {
        "accepted_spans": $AFTER_ACC_SPANS - $BEFORE_ACC_SPANS,
        "sent_spans": $AFTER_SENT_SPANS - $BEFORE_SENT_SPANS,
        "accepted_metric_points": $AFTER_ACC_POINTS - $BEFORE_ACC_POINTS,
        "sent_metric_points": $AFTER_SENT_POINTS - $BEFORE_SENT_POINTS,
        "accepted_log_records": $AFTER_ACC_LOGS - $BEFORE_ACC_LOGS,
        "sent_log_records": $AFTER_SENT_LOGS - $BEFORE_SENT_LOGS,
    },
    "backends": {
        "vm_total_series_before": $BEFORE_VM_SERIES,
        "vm_total_series_after": $AFTER_VM_SERIES,
        "vm_series_created": $AFTER_VM_SERIES - $BEFORE_VM_SERIES,
        "vm_cardinality_bomb_series_before": $BEFORE_BOMB_SERIES,
        "vm_cardinality_bomb_series_after": $AFTER_BOMB_SERIES,
        "vm_cardinality_bomb_series_created": $AFTER_BOMB_SERIES - $BEFORE_BOMB_SERIES,
        "loki_lines_in_window": $LOKI_LINES,
    },
    "memory_mib": {
        "collector": $MEM_COLLECTOR,
        "victoriametrics": $MEM_VM,
        "loki": $MEM_LOKI,
        "tempo": $MEM_TEMPO,
    },
}

def pct(sent, accepted):
    if not accepted:
        return None
    return round((1 - sent / accepted) * 100, 1)

c = d["collector"]
d["reduction_pct"] = {
    "spans": pct(c["sent_spans"], c["accepted_spans"]),
    "metric_points": pct(c["sent_metric_points"], c["accepted_metric_points"]),
    "log_records": pct(c["sent_log_records"], c["accepted_log_records"]),
}

with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, indent=2)

def row(label, value):
    print(f"  {label:<34} {value}")

print()
print(f"  ARM: {arm}   flood {d['profile']['duration_seconds']}s "
      f"({d['profile']['logs_per_second']} logs/s, "
      f"{d['profile']['unique_labels_per_second']} unique labels/s)")
print("  " + "-" * 60)
row("spans      accepted -> sent", f"{c['accepted_spans']} -> {c['sent_spans']}")
row("metric pts accepted -> sent", f"{c['accepted_metric_points']} -> {c['sent_metric_points']}")
row("log recs   accepted -> sent", f"{c['accepted_log_records']} -> {c['sent_log_records']}")
print("  " + "-" * 60)
for k, v in d["reduction_pct"].items():
    row(f"reduction: {k}", "n/a" if v is None else f"{v}%")
print("  " + "-" * 60)
b = d["backends"]
row("VM series created", b["vm_series_created"])
row("VM cardinality-bomb series created", b["vm_cardinality_bomb_series_created"])
row("Loki lines in window", b["loki_lines_in_window"])
print("  " + "-" * 60)
m = d["memory_mib"]
row("memory MiB (col/vm/loki/tempo)",
    f"{m['collector']} / {m['victoriametrics']} / {m['loki']} / {m['tempo']}")
print()
PYEOF

ok "written: ${RESULT#"$REPO_ROOT/"}"
