#!/usr/bin/env bash
# Shared helpers for scripts/. Sourced, not executed.
#
# These scripts are the canonical entrypoints for this repo (the Makefile is a thin
# alias layer). They are written to run identically in Git Bash on Windows and in
# bash on Linux/CI, which is why path handling below is more careful than it looks.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Git Bash on Windows rewrites POSIX-looking paths in argv before handing them to
# docker.exe, which corrupts -v and container-side paths. MSYS_NO_PATHCONV=1 turns
# that off; `pwd -W` gives the Windows-native path docker actually wants.
host_path() {
  if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "cygwin" ]]; then
    (cd "$1" && pwd -W)
  else
    (cd "$1" && pwd)
  fi
}

# Docker needs MSYS path conversion OFF (it mangles -v and container-side paths),
# but native git.exe needs it ON (it cannot resolve a /c/... path, so `git -C` fails
# outright). So this is applied per-invocation rather than exported globally —
# exporting it silently breaks every git call in these scripts.
docker_() {
  if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "cygwin" ]]; then
    MSYS_NO_PATHCONV=1 docker "$@"
  else
    docker "$@"
  fi
}

# --- output ---------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YLW=""; C_DIM=""; C_RST=""
fi

info() { printf '%s==>%s %s\n' "$C_DIM" "$C_RST" "$*"; }
ok()   { printf '%s ok %s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YLW" "$C_RST" "$*" >&2; }
die()  { printf '%sfail%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# --- env ------------------------------------------------------------------
# Load .env if present, falling back to .env.example so a fresh clone works
# without a copy step. Values already in the environment win.
load_env() {
  local f="$REPO_ROOT/.env"
  [[ -f "$f" ]] || f="$REPO_ROOT/.env.example"
  set -a
  # shellcheck disable=SC1090
  source "$f"
  set +a
}

# Not yet implemented — used by scripts belonging to a later phase, so the repo
# never ships a script that silently pretends to have done something.
todo_phase() {
  die "not implemented yet — lands in Phase $1 (see plan.md)"
}

# --- compose --------------------------------------------------------------
# Create .env from .env.example on first run. Compose only auto-loads `.env`, so
# without this every ${VAR} in docker-compose.yml would silently expand to empty --
# which shows up as a container started with no image tag rather than a clear error.
ensure_env() {
  if [[ ! -f "$REPO_ROOT/.env" ]]; then
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    info "created .env from .env.example"
    return
  fi

  # A .env from an earlier phase can be missing keys added since. Compose expands an
  # unset ${VAR} to an empty string WITHOUT erroring, which surfaces as a container
  # started with a blank image tag or a port mapping of ":8428" -- confusing failures
  # a long way from the real cause. Name the drift instead of letting it be silent.
  local missing=() key
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    grep -qE "^${key}=" "$REPO_ROOT/.env" || missing+=("$key")
  done < <(sed -nE 's/^([A-Z_][A-Z0-9_]*)=.*/\1/p' "$REPO_ROOT/.env.example")

  if (( ${#missing[@]} )); then
    warn ".env is missing ${#missing[@]} key(s) present in .env.example:"
    printf '       %s\n' "${missing[@]}" >&2
    warn "add them, or delete .env and let it be recreated"
  fi
}

# Always invoked from the repo root so compose resolves docker-compose.yml, .env and
# the relative bind-mount paths in it consistently. Overlays are opt-in via
# COMPOSE_OVERLAYS (space-separated filenames).
compose() {
  local args=(-f docker-compose.yml)
  local overlay
  for overlay in ${COMPOSE_OVERLAYS:-}; do
    args+=(-f "$overlay")
  done
  ( cd "$REPO_ROOT" && docker compose "${args[@]}" "$@" )
}
