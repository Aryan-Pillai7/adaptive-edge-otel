#!/usr/bin/env bash
# Drive synthetic telemetry at the Collector with otel/telemetrygen.
#
#   scripts/telemetrygen.sh                      all three signals, small burst
#   scripts/telemetrygen.sh traces               one signal
#   scripts/telemetrygen.sh traces --traces 500  extra flags pass through
#
# Runs on the compose network and targets the Collector by service name, so this
# exercises the same path the real app will take in Phase 3 -- not a shortcut
# straight to the backends.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env
load_env

SIGNALS=("traces" "metrics" "logs")
if [[ $# -gt 0 && "$1" != -* ]]; then
  SIGNALS=("$1"); shift
fi

# Tag every run so a verification query can find exactly this run's data and not
# something left over from a previous one. Exported for verify-pipeline.sh.
RUN_ID="${TELEMETRYGEN_RUN_ID:-run-$(date +%s)}"
COUNT="${TELEMETRYGEN_COUNT:-20}"
SERVICE="${TELEMETRYGEN_SERVICE:-telemetrygen-probe}"

network="$(compose ps --format '{{.Name}}' collector >/dev/null 2>&1 && echo ok || echo '')"
[[ -n "$network" ]] || die "collector is not running — start it with scripts/up.sh"

NET="$(docker inspect aeo-collector --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')"
[[ -n "$NET" ]] || die "could not determine the collector's docker network"

info "run id: $RUN_ID  (service=$SERVICE, count=$COUNT per signal)"

for sig in "${SIGNALS[@]}"; do
  # --otlp-attributes sets RESOURCE attributes (shared by the whole run);
  # --telemetry-attributes sets them per item. run_id goes on the resource so it
  # survives into VM as a label, Loki as structured metadata, and Tempo as a
  # resource-level tag -- one marker, queryable in all three backends.
  args=(
    --otlp-endpoint "collector:4317"
    --otlp-insecure
    --service "$SERVICE"
    --otlp-attributes "run_id=\"$RUN_ID\""
    --rate 0
  )
  case "$sig" in
    traces)  args+=(--traces "$COUNT") ;;
    metrics) args+=(--metrics "$COUNT" --metric-type Sum) ;;
    logs)    args+=(--logs "$COUNT") ;;
  esac

  info "generating $sig"
  docker_ run --rm --network "$NET" "$TELEMETRYGEN_IMAGE" "$sig" "${args[@]}" "$@" \
    >/dev/null 2>&1 || die "telemetrygen $sig failed"
  ok "$sig sent"
done

echo
ok "run id: $RUN_ID"
echo "$RUN_ID" > "$REPO_ROOT/.last-run-id"
