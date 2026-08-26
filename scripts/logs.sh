#!/usr/bin/env bash
# Tail logs for the stack, or one service: scripts/logs.sh loki
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env
compose logs --tail=100 -f "$@"
