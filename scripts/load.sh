#!/usr/bin/env bash
# Drive steady-state request traffic at the app.
#
#   scripts/load.sh [duration_seconds] [requests_per_second]
#
# This is the *normal* traffic an incident happens on top of, and it is what produces
# TRACES. The flood itself emits only logs and metric points, so without this running
# alongside it there would be no spans in the measurement window -- and Phase 4's
# tail-sampling policy would have nothing to act on, making the trace column of the
# before/after comparison meaningless.
#
# The mix matters as much as the volume: /api/orders returns a controlled fraction of
# 500s and slow responses, giving the sampler all three of its cases (error, slow,
# healthy) instead of only the one it samples down.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need curl
ensure_env
load_env

DURATION="${1:-${FLOOD_DURATION_SECONDS:-30}}"
RPS="${2:-${MEASURE_RPS:-10}}"
BASE="http://localhost:${APP_PORT}"

# Sub-second pacing without external tools. Integer math on purpose: `sleep 0.1` is
# supported by GNU sleep and by Git Bash, but the interval is derived rather than
# hardcoded so changing RPS actually changes the rate.
interval="$(python -c "print(round(1.0 / max(1, $RPS), 3))")"

info "driving ${RPS} req/s at ${BASE} for ${DURATION}s"

end=$(( $(date +%s) + DURATION ))
sent=0
while (( $(date +%s) < end )); do
  # Two thirds to the endpoint that can fail or be slow, one third to the cheap
  # always-200 endpoint. The healthy majority is what the ~5% baseline policy
  # samples down; the failures are what it keeps in full.
  curl -s -o /dev/null --max-time 5 "${BASE}/api/orders/ord-${sent}" &
  curl -s -o /dev/null --max-time 5 "${BASE}/api/orders/ord-b-${sent}" &
  curl -s -o /dev/null --max-time 5 "${BASE}/api/products" &
  sent=$(( sent + 3 ))
  sleep "$interval"
done

# Let the in-flight background requests finish so their spans are actually emitted
# rather than being cut off with the script.
wait 2>/dev/null || true

ok "sent ~${sent} requests"
