#!/usr/bin/env bash
# Bring the stack up.
#
#   scripts/up.sh              base stack
#   scripts/up.sh --profile ui  base stack + Grafana
#
# Anything passed through lands on `docker compose up`.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env

info "starting stack"
compose up -d --wait "$@" || die "stack failed to start — try: scripts/logs.sh"
ok "stack up"
echo
bash "$REPO_ROOT/scripts/smoke.sh"
