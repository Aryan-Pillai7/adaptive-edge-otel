#!/usr/bin/env bash
# Fast, dependency-light checks. Runs the same way locally and in CI.
#
# Intentionally does NOT duplicate scripts/validate.sh: this checks that YAML is
# well-formed, Python is clean, and shell parses. Whether a Collector config is
# semantically valid is the collector binary's job, not a linter's.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

failed=0
step() { info "$1"; }

# --- YAML well-formedness -------------------------------------------------
# Covers collector configs, storage configs, compose files and CI workflows.
step "yaml parse"
if command -v python >/dev/null 2>&1 && python -c "import yaml" 2>/dev/null; then
  # cd rather than `git -C`: see docker_() in lib.sh for why paths are fragile here.
  # No `|| true` — a failing git call must fail the lint, not read as "nothing to check".
  mapfile -t yamls < <(cd "$REPO_ROOT" && git ls-files '*.yaml' '*.yml')
  if [[ ${#yamls[@]} -eq 0 ]]; then
    warn "no tracked yaml files yet — skipping"
  else
    for y in "${yamls[@]}"; do
      # Compose and collector configs use ${env:VAR} / ${VAR}, which is valid YAML
      # scalar text, so a plain safe_load is the right check here.
      if python -c "import sys,yaml;list(yaml.safe_load_all(open(sys.argv[1],encoding='utf-8')))" "$REPO_ROOT/$y"; then
        ok "$y"
      else
        warn "$y is not valid yaml"; failed=1
      fi
    done
  fi
else
  warn "python+pyyaml unavailable — skipping yaml parse"
fi

# --- Python ---------------------------------------------------------------
step "ruff"
if command -v ruff >/dev/null 2>&1; then
  if compgen -G "$REPO_ROOT/app/**/*.py" >/dev/null || compgen -G "$REPO_ROOT/test/**/*.py" >/dev/null; then
    ruff check "$REPO_ROOT/app" "$REPO_ROOT/test" || failed=1
    ruff format --check "$REPO_ROOT/app" "$REPO_ROOT/test" || failed=1
  else
    warn "no python sources yet — skipping (lands in Phase 3)"
  fi
else
  warn "ruff not installed — skipping"
fi

# --- Shell ----------------------------------------------------------------
step "shell syntax"
for s in "$REPO_ROOT"/scripts/*.sh; do
  bash -n "$s" || { warn "$s has a syntax error"; failed=1; }
done
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "$REPO_ROOT"/scripts/*.sh || failed=1
else
  warn "shellcheck not installed locally — CI runs it"
fi

[[ $failed -eq 0 ]] || die "lint failed"
ok "lint clean"
