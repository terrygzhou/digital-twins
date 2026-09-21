#!/usr/bin/env bash
# 015 T001 (US1): local uninstall — one invocation on a host that was set up
# with scripts/bootstrap-local.sh + pip install digital-twins.
# Contract pinned by tests/unit/test_uninstall_script.py (R1): all external
# command invocations MUST route through run_cmd() so UNINSTALL_EXEC can
# intercept them in tests.
#
# Contract: specs/015-service-teardown/contracts/uninstall-cli.md
#   exit 0  success (nothing left to remove, or every step succeeded)
#   exit 1  a step failed and --force was not given (remediation printed)
#   exit 2  --tear-down-volumes was requested but a volume could not be
#           identified (named; nothing was removed)
#
# Host-neutral (NFR-13): no host paths, usernames, or interpreter pins.
# No image refs: docker-compose.yml remains the single source of image pins;
# this script tears down by compose service name only.
#
# What it does, in order (each step is a no-op when the target is absent,
# so a re-run on a clean host is safe and idempotent):
#   1. stop the local Docker stack (docker compose down) — no-op when no
#      stack is running; with --tear-down-volumes also removes the
#      named volumes (qdrant-data, neo4j-data, digital-twins-state)
#   2. uninstall the pip distribution (pip uninstall digital-twins
#      digital-twins-kb) — no-op when neither dist is installed;
#      pipx-managed installs are detected and reported, not removed
#   3. remove the machine-local config dir (~/.config/digital-twins) and
#      state dir (~/.digital-twins) — only with --remove-data (both
#      default off: an interactive confirmation is required)
set -euo pipefail

# All external commands route through run_cmd() so a test harness can set
# UNINSTALL_EXEC to a fake binary that logs argv and returns canned output.
# This wrapper is the single point of interception — do NOT invoke external
# commands directly (the tests depend on every call passing through here).
run_cmd() {
  if [ -n "${UNINSTALL_EXEC:-}" ]; then
    "$UNINSTALL_EXEC" "$@"
  else
    "$@"
  fi
}

# --- constants ---------------------------------------------------------------

COMPOSE_FILE="docker-compose.yml"
# Named volumes (compose service -> named volume). Tearing them down removes
# all ingested content from the local stack; a plain `docker compose down`
# keeps them so a re-bootstrap resumes where it left off.
VOLUMES="qdrant-data neo4j-data digital-twins-state"
# Dist names (PEP 503). digital-twins-kb is the published distribution;
# the bare digital-twins name is kept as a legacy uninstall no-op so an
# install predating the rename still gets cleaned up.
DIST_NAMES="digital-twins digital-twins-kb"
# Machine-local dirs (the package's built-in defaults, overridable via the
# KB_CONFIG_DIR / KB_STATE_DIR env vars, mirroring
# digital_twins/config/loader.py).  The script refuses to delete a dir that
# is a symlink or that it does not own — see dir_owned_by_us().
DEFAULT_CONFIG_DIR="$HOME/.config/digital-twins"
DEFAULT_STATE_DIR="$HOME/.digital-twins"

# Exit codes (contract-pinned).
EXIT_OK=0
EXIT_FAILED=1
EXIT_VOLUMES=2

# --- flags -------------------------------------------------------------------

TEAR_DOWN_VOLUMES=0
REMOVE_DATA=0
FORCE=0
SKIP_DOCKER=0

usage() {
  cat <<'USAGE'
Usage: uninstall-local.sh [--tear-down-volumes | --remove-data | --force | --skip-docker | --help]

Reverses what scripts/bootstrap-local.sh + pip install digital-twins set
up on this host.  Every step is a no-op when its target is absent, so the
script is safe to re-run and safe on a host that never had anything
installed (it prints what it would have removed and exits 0).

Options:
  --tear-down-volumes   Also `docker compose down -v`: removes the named
                       volumes (qdrant-data, neo4j-data, digital-twins-state).
                       All ingested content is lost.  Off by default.
  --remove-data         Also remove the machine-local config dir
                       (KB_CONFIG_DIR or ~/.config/digital-twins) and state
                       dir (KB_STATE_DIR or ~/.digital-twins).  Off by
                       default; requires an interactive "yes" confirmation
                       (or --force, which skips every confirmation).
  --force               Skip every interactive confirmation.
  --skip-docker         Skip the docker compose down step entirely (for
                       hosts that use cloud/external backends).
  --help                Show this help.

Environment overrides:
  UNINSTALL_TIMEOUT_S  Max seconds to wait for docker compose down (default 60).
USAGE
}

# --- helpers -----------------------------------------------------------------

# Print a line to stderr with the script prefix (stdout is kept free for the
# final "removed / kept / absent" report).
note() {
  echo "uninstall: $*" >&2
}

# Confirm with the user unless --force.  Returns 0 to proceed, 1 to skip the
# step.  An EOF on stdin (non-interactive) is treated as "no".
confirm() {
  local prompt="$1"
  if [ "$FORCE" -eq 1 ]; then
    return 0
  fi
  local answer
  printf 'uninstall: %s [y/N] ' "$prompt"
  IFS= read -r answer || answer="n"
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

# Detect whether a pip distribution is installed (for every name in
# DIST_NAMES).  Returns 0 when at least one is installed, 1 otherwise.
# pipx-managed installs are detected separately and reported (not removed
# here: pipx has its own `pipx uninstall`).
pip_installed() {
  local name
  for name in $DIST_NAMES; do
    if run_cmd pip show "$name" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

# Detect a pipx-managed install of the CLI (the `digital-twins` symlink in
# the pipx binary dir).  Returns 0 when found.
pipx_managed() {
  run_cmd pipx list digital-twins >/dev/null 2>&1
}

# A dir "owned by us" when it exists, is a plain directory (not a symlink),
# and its basename matches the digital-twins dir name — the guard against
# deleting an unrelated path that someone pointed the env var at.
dir_owned_by_us() {
  local dir="$1"
  [ -n "$dir" ] || return 1
  [ -d "$dir" ] || return 1
  # A symlink must not be followed (deleting the link vs. its target is
  # different things; refuse both).
  [ -L "$dir" ] && return 1
  case "$dir" in
    */digital-twins|*/digital-twins/|digital-twins|\
    */.digital-twins|*/.digital-twins/|.digital-twins) return 0 ;;
    *) return 1 ;;
  esac
}

# --- main --------------------------------------------------------------------

main() {
  # Arg parsing.
  for arg in "$@"; do
    case "$arg" in
      --tear-down-volumes) TEAR_DOWN_VOLUMES=1 ;;
      --remove-data)     REMOVE_DATA=1 ;;
      --force)           FORCE=1 ;;
      --skip-docker)     SKIP_DOCKER=1 ;;
      --help|-h)         usage; return 0 ;;
      *)
        note "unknown argument: $arg (use --help)"
        return 1
        ;;
    esac
  done

  local removed=""
  local kept=""
  local absent=""

  # --- 1) docker compose down ------------------------------------------------
  if [ "$SKIP_DOCKER" -eq 1 ]; then
    note "docker: skipped (--skip-docker)."
    kept="$kept docker stack (skipped)"
  else
    # Is the stack even running?  `docker compose down` on a host with no
    # running containers is a fast no-op, but it still needs the compose
    # file to exist in CWD.  Without the compose file there is nothing to
    # tear down from *this* project — note it and move on.
    if [ ! -f "$COMPOSE_FILE" ]; then
      note "docker: $COMPOSE_FILE not found in CWD — assuming no local stack from this project; skipping docker compose down."
      kept="$kept docker stack (no compose file in CWD)"
    else
      if [ "$TEAR_DOWN_VOLUMES" -eq 1 ]; then
        if ! confirm "tear down the docker stack AND remove all named volumes ($VOLUMES)? all ingested content is lost"; then
          note "docker: skipped (not confirmed)."
          kept="$kept docker stack + volumes (not confirmed)"
        else
          if run_cmd docker compose -f "$COMPOSE_FILE" down -v; then
            note "docker: stack torn down, volumes removed."
            removed="$removed docker stack + volumes"
          else
            note "ERROR: docker compose down -v failed — the volumes could not be removed."
            return "$EXIT_VOLUMES"
          fi
        fi
      else
        if ! confirm "stop the docker stack (keep volumes)?"; then
          note "docker: skipped (not confirmed)."
          kept="$kept docker stack (not confirmed; re-run with --force or answer 'y')"
        else
          if run_cmd docker compose -f "$COMPOSE_FILE" down; then
            note "docker: stack stopped (volumes kept)."
            removed="$removed docker stack (volumes kept)"
          else
            note "ERROR: docker compose down failed — the stack was not stopped.  Remediation: run 'docker compose -f $COMPOSE_FILE down' manually."
            if [ "$FORCE" -eq 1 ]; then
              note "continuing past the docker step (--force)."
              kept="$kept docker stack (compose down failed)"
            else
              return "$EXIT_FAILED"
            fi
          fi
        fi
      fi
    fi
  fi

  # --- 2) pip uninstall -------------------------------------------------------
  if pipx_managed; then
    note "pipx: 'digital-twins' is managed by pipx — run 'pipx uninstall digital-twins' to remove it (this script does not touch pipx installs)."
    kept="$kept pipx-managed install (run: pipx uninstall digital-twins)"
  elif pip_installed; then
    if ! confirm "uninstall the pip distribution(s): $DIST_NAMES"; then
      note "pip: skipped (not confirmed)."
      kept="$kept pip distribution ($DIST_NAMES)"
    else
      if run_cmd pip uninstall -y $DIST_NAMES; then
        note "pip: $DIST_NAMES uninstalled."
        removed="$removed pip distribution ($DIST_NAMES)"
      else
        note "ERROR: pip uninstall failed.  Remediation: run 'pip uninstall -y $DIST_NAMES' manually (a broken install may need 'pip uninstall -y' per name)."
        if [ "$FORCE" -eq 1 ]; then
          note "continuing past the pip step (--force)."
          kept="$kept pip distribution ($DIST_NAMES, uninstall failed)"
        else
          return "$EXIT_FAILED"
        fi
      fi
    fi
  else
    note "pip: digital-twins / digital-twins-kb not installed via pip — nothing to uninstall."
    absent="$absent pip distribution"
  fi

  # --- 3) config + state dirs -------------------------------------------------
  local config_dir="${KB_CONFIG_DIR:-$DEFAULT_CONFIG_DIR}"
  local state_dir="${KB_STATE_DIR:-$DEFAULT_STATE_DIR}"
  if [ "$REMOVE_DATA" -eq 1 ]; then
    for dir in "$config_dir" "$state_dir"; do
      if ! dir_owned_by_us "$dir"; then
        note "data: $dir is not a plain 'digital-twins' dir we created (missing, a symlink, or renamed) — leaving it untouched."
        kept="$kept data dir $dir (not owned)"
        continue
      fi
      if ! confirm "remove $dir (config: kb.local.yml, secrets; state: state.db, admin-credentials.txt)"; then
        note "data: $dir kept (not confirmed)."
        kept="$kept data dir $dir (not confirmed)"
        continue
      fi
      if run_cmd rm -rf "$dir"; then
        note "data: removed $dir."
        removed="$removed data dir $dir"
      else
        note "ERROR: failed to remove $dir.  Remediation: 'rm -rf $dir' manually (check permissions)."
        if [ "$FORCE" -eq 1 ]; then
          note "continuing past the data step (--force)."
          kept="$kept data dir $dir (rm failed)"
        else
          return "$EXIT_FAILED"
        fi
      fi
    done
  else
    note "data: config/state dirs kept (re-run with --remove-data to delete them)."
    kept="$kept config dir $config_dir; state dir $state_dir"
  fi

  # --- final report ------------------------------------------------------------
  if [ -n "$removed" ]; then
    echo "uninstall: removed:$removed" >&2
  fi
  if [ -n "$kept" ]; then
    echo "uninstall: kept:$kept" >&2
  fi
  if [ -n "$absent" ]; then
    echo "uninstall: absent:$absent" >&2
  fi
  echo "uninstall: done." >&2
  return 0
}

main "$@"
