#!/usr/bin/env bash
# Validate every Collector config in collector/config/.
#
# Runs the real otelcol-contrib binary in the pinned image rather than a YAML
# schema check: only the binary knows whether a processor exists in this build and
# whether its settings parse. A config that lints clean but fails `validate` would
# still crash-loop at `docker compose up`.

# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
need docker

CONFIG_DIR="$REPO_ROOT/collector/config"
shopt -s nullglob
configs=("$CONFIG_DIR"/*.yaml)
shopt -u nullglob

[[ ${#configs[@]} -gt 0 ]] || die "no configs found in collector/config/"

mount_src="$(host_path "$CONFIG_DIR")"
failed=0

for cfg in "${configs[@]}"; do
  name="$(basename "$cfg")"
  info "validating $name"
  # Env vars referenced via ${env:...} must be present or expansion fails, so the
  # loaded .env is passed through to the container.
  if docker_ run --rm \
      -v "${mount_src}:/cfg:ro" \
      -e "COLLECTOR_MEMORY_LIMIT_MIB=${COLLECTOR_MEMORY_LIMIT_MIB}" \
      -e "COLLECTOR_MEMORY_SPIKE_MIB=${COLLECTOR_MEMORY_SPIKE_MIB}" \
      -e "TAIL_SAMPLING_SLOW_THRESHOLD_MS=${TAIL_SAMPLING_SLOW_THRESHOLD_MS:-500}" \
      -e "TAIL_SAMPLING_BASELINE_PCT=${TAIL_SAMPLING_BASELINE_PCT:-5}" \
      -e "TAIL_SAMPLING_DECISION_WAIT=${TAIL_SAMPLING_DECISION_WAIT:-10s}" \
      "$COLLECTOR_IMAGE" validate --config "/cfg/$name"; then
    ok "$name"
  else
    warn "$name failed validation"
    failed=1
  fi
done

[[ $failed -eq 0 ]] || die "collector config validation failed"
ok "all collector configs valid"
