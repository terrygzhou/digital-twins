#!/usr/bin/env bash
# install-local.sh — Option A: one-shot installer for a non-technical user.
#
# One invocation on a clean host to a working digital-twins install:
#   1. find a usable Python (>=3.11)
#   2. create an isolated venv under the user's home
#   3. pip install the chosen extras
#   4. run `digital-twins setup` (the first-run wizard)
#   5. optionally run the first ingest
#
# Contract (mirrors bootstrap-local.sh / uninstall-local.sh conventions):
#   - Host-neutral (NFR-13): no host paths, usernames, or interpreter pins.
#     $HOME is resolved at runtime; the only pins are the Python >=3.11 floor
#     (required by the package) and the PyPI distribution name.
#   - Idempotent & safe to re-run: every step is a no-op when its target
#     already exists, so re-running on an installed host just re-validates.
#   - All external command invocations route through run_cmd() so the
#     INSTALL_EXEC env can intercept them in tests (same pattern as
#     BOOTSTRAP_EXEC / UNINSTALL_EXEC).
#
# Exit codes:
#   0  success (install + setup completed)
#   1  a step failed and --force was not given (remediation printed)
#   2  no usable Python >=3.11 was found
#   3  `digital-twins setup` reported a failing health check
#   5  `digital-twins setup` could not resolve cloud endpoints
#
# Usage:
#   bash scripts/install-local.sh [options]
#
#   Options:
#     --extras LIST       Comma-separated extras to install (default: mcp).
#                         Use "" for the base install with no extras.
#     --cloud             Pass --cloud to `setup` (force cloud backend mode)
#     --skip-services     Pass --skip-services to `setup`
#     --run-ingest        After setup, run `digital-twins run --source fs`
#     --no-setup          Stop after the pip install; do not run the wizard
#     --python PATH       Use this specific python interpreter
#     --dist PATH|NAME    Install this wheel/directory instead of the PyPI
#                         dist (a local checkout or a local .whl).
#     --help              Show this help.
#
# Environment overrides:
#   INSTALL_EXEC       Path to a fake binary that intercepts run_cmd() calls.
#   INSTALL_EXEC_LOG   Where the fake writes its JSONL argv log.
#   INSTALL_TIMEOUT_S  Max seconds to wait on the pip install (default 600).
set -euo pipefail

# All external commands route through run_cmd() so a test harness can set
# INSTALL_EXEC to a fake binary that logs argv and returns canned output.
run_cmd() {
  if [ -n "${INSTALL_EXEC:-}" ]; then
    "$INSTALL_EXEC" "$@"
  else
    "$@"
  fi
}

# --- constants ---------------------------------------------------------------
DIST_NAME="digital-twins"
MIN_PY_MINOR="11"

# --- flags -------------------------------------------------------------------
EXTRAS="mcp"
DIST_OVERRIDE=""
CLOUD=0
SKIP_SERVICES=0
RUN_INGEST=0
NO_SETUP=0
PYTHON_OVERRIDE=""

usage() {
  sed -n '1,50p' "$0" | grep -E '^#( |$)' | sed 's/^# \{0,2\}//'
}

note() { echo "install: $*" >&2; }
fail() { note "ERROR: $*"; exit 1; }

# --- helpers -----------------------------------------------------------------

# Print "major.minor" for a python interpreter, or empty on failure.
py_version() {
  run_cmd "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

# Return 0 when a python interpreter reports >= 3.${MIN_PY_MINOR}.
python_ok() {
  local interp="$1" v maj min
  command -v "$interp" >/dev/null 2>&1 || return 1
  v="$(py_version "$interp" || true)"
  [ -n "$v" ] || return 1
  maj="${v%%.*}"
  min="${v#*.}"
  if [ "$maj" -gt 3 ] 2>/dev/null; then return 0; fi
  if [ "$maj" -eq 3 ] && [ "$min" -ge "$MIN_PY_MINOR" ] 2>/dev/null; then
    return 0
  fi
  return 1
}

# Find a usable python: explicit override, then python3, then python,
# then common absolute locations.
find_python() {
  if [ -n "$PYTHON_OVERRIDE" ]; then
    if python_ok "$PYTHON_OVERRIDE"; then echo "$PYTHON_OVERRIDE"; return 0; fi
    note "requested python '$PYTHON_OVERRIDE' is not a usable interpreter (needs >= 3.${MIN_PY_MINOR})."
    return 1
  fi
  local cand p
  for cand in python3 python; do
    if python_ok "$cand"; then echo "$cand"; return 0; fi
  done
  for p in /usr/bin/python3 /usr/local/bin/python3; do
    [ -x "$p" ] || continue
    if python_ok "$p"; then echo "$p"; return 0; fi
  done
  return 1
}

# --- main --------------------------------------------------------------------
main() {
  # Arg parsing (index-based so value-taking flags are explicit).
  local args=("$@")
  local i=0
  while [ "$i" -lt "${#args[@]}" ]; do
    case "${args[$i]}" in
      --extras)        i=$((i+1)); EXTRAS="${args[$i]:-}" ;;
      --cloud)         CLOUD=1 ;;
      --skip-services) SKIP_SERVICES=1 ;;
      --run-ingest)    RUN_INGEST=1 ;;
      --no-setup)      NO_SETUP=1 ;;
      --python)        i=$((i+1)); PYTHON_OVERRIDE="${args[$i]:-}" ;;
      --dist)          i=$((i+1)); DIST_OVERRIDE="${args[$i]:-}" ;;
      --help|-h)       usage; return 0 ;;
      *) fail "unknown argument: ${args[$i]} (use --help)" ;;
    esac
    i=$((i+1))
  done

  # --- 1) find python -------------------------------------------------------
  local py
  if ! py="$(find_python)"; then
    if [ -n "$PYTHON_OVERRIDE" ]; then
      # find_python already printed the specific remediation via fail().
      :
    else
      note "no Python >= 3.${MIN_PY_MINOR} found on PATH."
      note "Install one (e.g. your OS package manager's python3) and re-run."
    fi
    exit 2
  fi
  note "using python: $py ($(py_version "$py"))"

  # --- 2) create an isolated venv ------------------------------------------
  local venv_dir="$HOME/.digital-twins/.venv"
  if [ -d "$venv_dir/bin" ] && [ -x "$venv_dir/bin/pip" ]; then
    note "venv already present at $venv_dir — reusing it."
  else
    note "creating venv at $venv_dir ..."
    run_cmd "$py" -m venv "$venv_dir"
  fi
  local venv_pip="$venv_dir/bin/pip"
  local venv_bin="$venv_dir/bin/$DIST_NAME"

  # --- 3) pip install -------------------------------------------------------
  local extra_spec
  if [ -n "$DIST_OVERRIDE" ]; then
    extra_spec="$DIST_OVERRIDE"
  elif [ -n "$EXTRAS" ]; then
    extra_spec="${DIST_NAME}[${EXTRAS}]"
  else
    extra_spec="${DIST_NAME}"
  fi
  note "installing $extra_spec (this can take a while) ..."
  if ! run_cmd "$venv_pip" install --quiet "$extra_spec"; then
    fail "pip install failed. Check your network / PyPI access and re-run."
  fi

  # --- 4) run the setup wizard ---------------------------------------------
  local setup_rc=0
  if [ "$NO_SETUP" -eq 0 ]; then
    local setup_args=()
    [ "$CLOUD" -eq 1 ]         && setup_args+=("--cloud")
    [ "$SKIP_SERVICES" -eq 1 ] && setup_args+=("--skip-services")
    note "running the first-run wizard: $venv_bin setup${setup_args[*]:+ ${setup_args[*]}}"
    run_cmd "$venv_bin" setup "${setup_args[@]}" || setup_rc=$?
    case "$setup_rc" in
      0) note "setup: all health checks passed." ;;
      1|3|5)
        case "$setup_rc" in
          1) note "setup: a health check failed (see the report above); re-run '$venv_bin setup' after fixing the endpoint." ;;
          3) note "setup: the local Docker stack did not become healthy; re-run after it is up." ;;
          5) note "setup: cloud endpoints could not be resolved (see which KB_* var is empty above)." ;;
        esac
        exit "$setup_rc"
        ;;
      *) fail "setup exited unexpectedly ($setup_rc)." ;;
    esac
  else
    note "skipping setup (--no-setup)."
  fi

  # --- 5) optional first ingest --------------------------------------------
  if [ "$RUN_INGEST" -eq 1 ]; then
    note "running first ingest: $venv_bin run --source fs"
    run_cmd "$venv_bin" run --source fs
  fi

  echo
  note "done. Next: $venv_bin run --source fs   (or the web UI: $venv_bin web)"
  echo "activate anytime with:  source $venv_dir/bin/activate"
  return 0
}

main "$@"
