#!/usr/bin/env bash
# Fast, dependency-light checks. Runs the same way locally and in CI.
#
# Intentionally does NOT duplicate scripts/validate.sh: this checks that YAML is
# well-formed, Python is clean, and shell parses. Whether a Collector config is
# semantically valid is the collector binary's job, not a linter's.

# Tells shellcheck -x where to find the sourced file. The path is built at
# runtime, so without this it looks for ./lib.sh relative to the CWD, fails to
# find it, and emits SC1091 -- which is only "info" severity but still exits
# non-zero and fails the lint.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# Only needed for SHELLCHECK_IMAGE. Falls back to .env.example, so this works on a
# fresh clone and in CI without anyone having created a .env first.
load_env

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
# -x follows sourced files, so lib.sh is analysed in context rather than skipped.
# Each script carries a `# shellcheck source-path=SCRIPTDIR` directive, because the
# source path is built at runtime and shellcheck cannot resolve it otherwise.
#
# Falls back to the pinned Docker image when the binary is not installed. That matters:
# The binary is not available on Windows/Git Bash, so before this fallback existed
# the check ran ONLY in CI -- and a lint you cannot run locally is a lint you cannot
# iterate on. It failed CI twice for a finding that takes seconds to fix.
#
# NOTE: never start a prose comment with the word shellcheck immediately after the
# hash -- it is parsed as a directive and fails with SC1072/SC1073.
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x "$REPO_ROOT"/scripts/*.sh || failed=1
elif command -v docker >/dev/null 2>&1; then
  info "shellcheck not installed — using $SHELLCHECK_IMAGE"

  # Basenames expanded on the HOST. The shellcheck image has no shell, so a glob
  # passed through would arrive at the binary unexpanded and be treated as a literal
  # filename.
  sc_files=()
  for script in "$REPO_ROOT"/scripts/*.sh; do
    sc_files+=("$(basename "$script")")
  done

  # Checks the WORKING TREE, not the committed content: a linter has to see the change
  # you are about to commit, or it can only report problems you already shipped.
  if ! docker_ run --rm -v "$(host_path "$REPO_ROOT/scripts"):/mnt" -w /mnt \
       "$SHELLCHECK_IMAGE" -x "${sc_files[@]}"; then
    failed=1
    # CRLF in the working tree produces a wall of parse errors that CI never sees,
    # because .gitattributes normalises to LF on commit. Name that specifically rather
    # than leaving someone to decode SC1017 line by line.
    #
    # Uses awk rather than a carriage-return grep: the `grep -lU` form prints nothing
    # yet still exits 0 in Git Bash, so it reported CRLF on a tree that was entirely LF.
    for script in "$REPO_ROOT"/scripts/*.sh; do
      if awk '/\r$/ { found = 1 } END { exit !found }' "$script"; then
        warn "CRLF line endings in the working tree (e.g. $(basename "$script"))"
        warn "fix with: git add --renormalize . && git checkout -- ."
        break
      fi
    done
  fi
else
  warn "shellcheck unavailable (no binary, no docker) — skipping"
fi

[[ $failed -eq 0 ]] || die "lint failed"
ok "lint clean"
