#!/usr/bin/env bash
# install-local.sh — Option A: one-shot installer for a non-technical user.
#
# One invocation on a clean host to a working digital-twins install:
#   1. find a usable Python (>=3.11)
#   2. create an isolated venv under $HOME/.digital-twins/.venv —
#      via `uv venv` when the `uv` binary is on PATH, else stdlib venv
#   3. pip install the chosen extras (`uv pip install` when the venv
#      was created by uv, else the venv's own pip)
#   4. run `digital-twins setup` (the first-run wizard) — only with
#      --with-setup; by default the installer stops after the pip install
#      and prints "run `digital-twins setup`"
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
#   0  success (install completed; with --with-setup: install + setup completed)
#   1  a step failed (remediation printed)
#   2  no usable Python >=3.11 was found
#   3  `digital-twins setup` reported a failing health check (with --with-setup)
#   5  `digital-twins setup` could not resolve cloud endpoints (with --with-setup)
#   6  `digital-twins setup` was interrupted (Ctrl-C / EOF at a prompt) (with --with-setup)
#
# Usage:
#   bash scripts/install-local.sh [options]
#
#   Options:
#     --extras LIST       Comma-separated extras to install (default: mcp).
#                         Use "" for the base install with no extras.
#     --with-setup        Run the first-run wizard (digital-twins setup) after
#                         the pip install; by default the installer stops after
#                         installing.
#     --cloud             Pass --cloud to `setup` (force cloud backend mode)
#                         (with --with-setup)
#     --cloud-env         Pass --cloud-env to `setup` (non-interactive cloud
#                         mode: endpoints come from the KB_* env vars, never
#                         a prompt; exit 5 names the missing var(s))
#                         (with --with-setup)
#     --skip-services     Pass --skip-services to `setup` (with --with-setup)
#     --run-ingest        After install (and after setup when --with-setup),
#                         run `digital-twins run --source fs`
#     --no-setup          No-op alias (the installer is install-only by default
#                         now); accepted for one release, prints a note.
#     --python PATH       Use this specific python interpreter
#     --dist PATH|NAME    Install this wheel/directory instead of the PyPI
#                         dist (a local checkout or a local .whl).
#     --help              Show this help.
#
# Environment overrides:
#   INSTALL_EXEC       Path to a fake binary that intercepts run_cmd() calls.
#   INSTALL_EXEC_LOG   Where the fake writes its JSONL argv log.
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
DIST_NAME="digital-twins-kb"
# The pip distribution name vs. the console-script name.  The dist
# publishes as digital-twins-kb on PyPI (bare digital-twins is name-blocked);
# the CLI it installs is still `digital-twins`.
CLI_NAME="digital-twins"
MIN_PY_MINOR="11"

# --- flags -------------------------------------------------------------------
EXTRAS="mcp"
DIST_OVERRIDE=""
CLOUD=0
CLOUD_ENV=0
SKIP_SERVICES=0
RUN_INGEST=0
NO_SETUP=0
WITH_SETUP=0
PYTHON_OVERRIDE=""

usage() {
  # Print the header comment block: every full-comment or blank line from
  # line 2 up to the first blank line before '# --- constants ---' (which
  # separates the header from the code; `set -euo pipefail` follows it, so
  # stopping at the blank line keeps --help free of code/docstring lines).
  # No line-count range, so growth of the header cannot leak into the body.
  sed -n '2,/^$/{ p; /^$/q; }' "$0" | grep -E '^#( |$)' | sed 's/^# \{0,2\}//'
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
      --cloud-env)     CLOUD_ENV=1 ;;
      --skip-services) SKIP_SERVICES=1 ;;
      --run-ingest)    RUN_INGEST=1 ;;
      --no-setup)      NO_SETUP=1 ;;
      --with-setup)    WITH_SETUP=1 ;;
      --python)        i=$((i+1)); PYTHON_OVERRIDE="${args[$i]:-}" ;;
      --dist)          i=$((i+1)); DIST_OVERRIDE="${args[$i]:-}" ;;
      --help|-h)       usage; return 0 ;;
      *) fail "unknown argument: ${args[$i]} (use --help)" ;;
    esac
    i=$((i+1))
  done

  # The --cloud / --cloud-env / --skip-services flags only mean anything
  # when the wizard runs.  Under the install-only default they are silently
  # ignored with a note (not an error — the user may have copy-pasted an
  # old command line).
  if [ "$WITH_SETUP" -eq 0 ]; then
    if [ "$CLOUD" -eq 1 ]; then
      note "--cloud is ignored without --with-setup (install-only default)."
    fi
    if [ "$CLOUD_ENV" -eq 1 ]; then
      note "--cloud-env is ignored without --with-setup (install-only default)."
    fi
    if [ "$SKIP_SERVICES" -eq 1 ]; then
      note "--skip-services is ignored without --with-setup (install-only default)."
    fi
  fi

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
  # Prefer uv when it is on PATH (host-neutral: we never install uv
  # itself — we only use it when the user already has it); fall back to
  # the stdlib venv module.
  local use_uv=0
  if [ -n "${FAKE_UV_PRESENT:-}" ] && [ -e "${FAKE_UV_PRESENT}" ]; then
    use_uv=1
  elif [ -z "${FAKE_UV_PRESENT:-}" ] && run_cmd command -v uv >/dev/null 2>&1; then
    use_uv=1
  fi
  if [ "$use_uv" -eq 1 ]; then
    note "using uv for the venv."
  fi
  local venv_dir="$HOME/.digital-twins/.venv"
  if [ -d "$venv_dir/bin" ] && [ -x "$venv_dir/bin/pip" ]; then
    note "venv already present at $venv_dir — reusing it."
  else
    if [ "$use_uv" -eq 1 ]; then
      note "creating venv at $venv_dir with uv ..."
      run_cmd uv venv --python "$py" "$venv_dir"
    else
      note "creating venv at $venv_dir ..."
      run_cmd "$py" -m venv "$venv_dir"
    fi
  fi
  local venv_pip="$venv_dir/bin/pip"
  local venv_bin="$venv_dir/bin/$CLI_NAME"
  # pip_cmd: `<venv>/bin/pip install …` for the stdlib path, or
  # `uv pip install --python <venv python>` for the uv path (uv-managed
  # venvs ship no pip).
  local pip_cmd=("$venv_pip" install)
  if [ "$use_uv" -eq 1 ]; then
    pip_cmd=(uv pip install --python "$venv_dir/bin/python")
  fi

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
  # No --quiet: a non-technical user gets visible progress (pip
  # download/copy lines) during the step that can take minutes.
  if ! run_cmd "${pip_cmd[@]}" "$extra_spec"; then
    fail "install failed. Check your network / PyPI access and re-run."
  fi

  # --- 4) run the setup wizard (only with --with-setup) ----------------------
  local setup_rc=0
  if [ "$WITH_SETUP" -eq 1 ]; then
    # The wizard prompts for endpoints when the backend is cloud; a
    # non-interactive stdin (this script piped in, or run in a pipeline)
    # cannot answer the prompts and the wizard exits 6.  Detect that up
    # front and tell the user the non-interactive path (--cloud-env with
    # KB_* env vars, or --no-setup + a later interactive run) so the
    # failure is a clear "set these vars and re-run", not a surprise.
    if [ ! -t 0 ] && [ "$CLOUD_ENV" -eq 0 ] && [ "$SKIP_SERVICES" -eq 0 ]; then
      note "stdin is not a terminal, so the wizard cannot ask questions."
      note "Non-interactive options:"
      note "  - set the cloud endpoint env vars (KB_QDRANT__URL etc.) and re-run with --cloud-env,"
      note "  - or run with --no-setup now, then '$venv_bin setup' in a real terminal."
      note "Continuing (the wizard will exit 6 if it cannot reach a backend)."
    fi
    # Build the flag string (empty-safe on bash 3.2 + set -u — no array
    # expansion at all, so macOS's shipped bash 3.2 cannot trip on it).
    local setup_flags=""
    [ "$CLOUD" -eq 1 ]         && setup_flags="${setup_flags} --cloud"
    [ "$CLOUD_ENV" -eq 1 ]     && setup_flags="${setup_flags} --cloud-env"
    [ "$SKIP_SERVICES" -eq 1 ] && setup_flags="${setup_flags} --skip-services"
    note "running the first-run wizard: $venv_bin setup${setup_flags}"
    run_cmd "$venv_bin" setup $setup_flags || setup_rc=$?
    case "$setup_rc" in
      0) note "setup: all health checks passed." ;;
      1|3|5|6)
        case "$setup_rc" in
          1) note "setup: a step failed (see the report above)."
         note "If a health check failed: fix the endpoint, then re-run '$venv_bin setup'."
         note "If the wizard was interrupted: re-run '$venv_bin setup' in a real terminal." ;;
          3) note "setup: the local Docker stack did not become healthy; re-run after it is up." ;;
          5) note "setup: cloud endpoints could not be resolved (see which KB_* var is empty above)." ;;
          6) note "setup: the wizard was interrupted before the backend was configured."
         note "To continue in a real terminal:  source $venv_dir/bin/activate && digital-twins setup"
         note "Or non-interactive: set the cloud endpoint env vars and re-run with --cloud-env." ;;
        esac
        exit "$setup_rc"
        ;;
      *) fail "setup exited unexpectedly ($setup_rc)." ;;
    esac
  else
    if [ "$NO_SETUP" -eq 1 ]; then
      note "--no-setup is a no-op: the installer is install-only by default now; run '$venv_bin setup' when you are ready."
    else
      note "install-only: the first-run wizard was skipped."
      note "run '$venv_bin setup' to configure backends, init the store, and create the admin account."
    fi
  fi

  # --- 5) optional first ingest --------------------------------------------
  if [ "$RUN_INGEST" -eq 1 ]; then
    note "running first ingest: $venv_bin run --source fs"
    run_cmd "$venv_bin" run --source fs
  fi

  echo
  echo "=== digital-twins is installed ==="
  echo
  echo "  1. Activate the environment (any terminal):"
  echo "       source $venv_dir/bin/activate"
  if [ "$WITH_SETUP" -eq 1 ]; then
    echo "  2. Run your first ingest:"
    echo "       digital-twins run --source fs"
    echo "  3. Open the web UI:"
    echo "       digital-twins web"
  else
    echo "  2. Configure (backends, store, admin account):"
    echo "       digital-twins setup"
    echo "  3. Run your first ingest:"
    echo "       digital-twins run --source fs"
    echo "  4. Open the web UI:"
    echo "       digital-twins web"
  fi
  echo
  echo "  Your admin credentials are in: $HOME/.digital-twins/admin-credentials.txt"
  echo "  (delete that file after your first login)"
  echo
  echo "  Uninstall:  see scripts/uninstall-local.sh in the repo (removes venv + state dir)"
  return 0
}

main "$@"
