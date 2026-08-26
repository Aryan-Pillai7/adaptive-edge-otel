#!/usr/bin/env bash
# Tear the stack down.
#
#   scripts/down.sh          stop containers, KEEP data volumes
#   scripts/down.sh --clean  stop containers and DELETE data volumes
#
# Volumes are kept by default so a restart does not silently discard the telemetry
# a measurement run just produced.

# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
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
