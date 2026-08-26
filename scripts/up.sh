#!/usr/bin/env bash
# Bring the stack up.
#
#   scripts/up.sh              base stack
#   scripts/up.sh --profile ui  base stack + Grafana
#
# Anything passed through lands on `docker compose up`.

# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env

info "starting stack"
compose up -d --wait "$@" || die "stack failed to start — try: scripts/logs.sh"
ok "stack up"
echo
bash "$REPO_ROOT/scripts/smoke.sh"
