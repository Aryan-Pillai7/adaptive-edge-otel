#!/usr/bin/env bash
# Tail logs for the stack, or one service: scripts/logs.sh loki
# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
ensure_env
compose logs --tail=100 -f "$@"
