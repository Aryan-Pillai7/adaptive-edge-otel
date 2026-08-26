#!/usr/bin/env bash
# Tear the stack down.
#
#   scripts/down.sh          stop containers, KEEP data volumes
#   scripts/down.sh --clean  stop containers and DELETE data volumes
#
# Volumes are kept by default so a restart does not silently discard the telemetry
# a measurement run just produced.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env

if [[ "${1:-}" == "--clean" ]]; then
  warn "removing data volumes — all stored telemetry will be lost"
  compose down --volumes --remove-orphans
  ok "stack down, volumes removed"
else
  compose down --remove-orphans
  ok "stack down (volumes kept — use --clean to remove)"
fi
