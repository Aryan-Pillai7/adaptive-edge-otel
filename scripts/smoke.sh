#!/usr/bin/env bash
# Readiness + resource budget check for the whole pipeline.
#
# Two questions, both of which must be yes:
#   1. Is each backend actually ready to serve? (not just "container running")
#   2. Is each backend inside its memory budget?
#
# (2) is not decoration. This project's claim is that edge processing keeps backends
# small; if a backend exceeds budget the claim is failing, so the smoke test fails
# with it rather than reporting a green stack.

# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env
load_env

failed=0

# Probes run from the host: the VictoriaMetrics image is distroless and has neither a
# shell nor curl, so an in-container healthcheck is not available for all three.
probe() {
  local name="$1" url="$2" expect="$3" tries="${4:-30}"
  local i body
  for (( i = 1; i <= tries; i++ )); do
    if body="$(curl -fsS --max-time 3 "$url" 2>/dev/null)"; then
      if [[ -z "$expect" || "$body" == *"$expect"* ]]; then
        ok "$name ready  ($url)"
        return 0
      fi
    fi
    sleep 2
  done
  warn "$name NOT ready after $((tries * 2))s  ($url)"
  [[ -n "${body:-}" ]] && warn "  last response: ${body:0:200}"
  failed=1
  return 1
}

info "readiness"
# The Collector is checked first and deliberately: if it is down, telemetry never
# reaches the backends and three green backends would be a misleading result.
probe "collector"       "http://localhost:${COLLECTOR_HEALTH_PORT}/"        ""
probe "victoriametrics" "http://localhost:${VICTORIAMETRICS_PORT}/health" ""
probe "loki"            "http://localhost:${LOKI_PORT}/ready"             "ready"
probe "tempo"           "http://localhost:${TEMPO_QUERY_PORT}/ready"      "ready"
probe "app"             "http://localhost:${APP_PORT}/health"             "ok"

# --- memory budget --------------------------------------------------------
# Compared against the limits in .env rather than a hardcoded number, so raising a
# budget is a deliberate, reviewable edit.
to_mib() { # accepts "200m" / "1g" / "512k" -> MiB
  local v="${1,,}"
  case "$v" in
    *g) echo $(( ${v%g} * 1024 )) ;;
    *m) echo "${v%m}" ;;
    *k) echo $(( ${v%k} / 1024 )) ;;
    *)  echo $(( v / 1048576 )) ;;
  esac
}

info "memory budget"
check_mem() {
  local cname="$1" budget_raw="$2"
  local budget usage_mib line
  budget="$(to_mib "$budget_raw")"

  line="$(docker stats --no-stream --format '{{.MemUsage}}' "$cname" 2>/dev/null)" || {
    warn "$cname not running"; failed=1; return 1; }

  # "123.4MiB / 200MiB" -> 123.4 -> integer MiB
  usage_mib="$(awk -F' / ' '{print $1}' <<<"$line" \
    | awk '{ v=$0; sub(/[A-Za-z]+$/,"",v);
             if ($0 ~ /GiB/) v*=1024; else if ($0 ~ /KiB/) v/=1024;
             printf "%d", v }')"

  local pct=$(( usage_mib * 100 / budget ))
  if (( usage_mib > budget )); then
    warn "$cname  ${usage_mib}MiB / ${budget}MiB  (${pct}%) — OVER BUDGET"
    failed=1
  elif (( pct > 85 )); then
    warn "$cname  ${usage_mib}MiB / ${budget}MiB  (${pct}%) — close to limit"
  else
    ok "$cname  ${usage_mib}MiB / ${budget}MiB  (${pct}%)"
  fi
}

check_mem aeo-app             "$APP_MEM_LIMIT"
check_mem aeo-collector       "$COLLECTOR_MEM_LIMIT"
check_mem aeo-victoriametrics "$VM_MEM_LIMIT"
check_mem aeo-loki            "$LOKI_MEM_LIMIT"
check_mem aeo-tempo           "$TEMPO_MEM_LIMIT"

echo
[[ $failed -eq 0 ]] || die "smoke check failed"
ok "pipeline healthy and within budget"
