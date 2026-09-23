#!/usr/bin/env bash
# install.sh — remote one-liner installer (the `curl -fsSL …/install.sh | bash`
# entry point). A non-technical user's whole journey is:
#
#   curl -fsSL https://raw.githubusercontent.com/terrygzhou/digital-twins/main/scripts/install.sh | bash
#
# What it does, in order (each step is a no-op when its target is present,
# so a re-run on an installed host just re-validates):
#   1. find a usable Python >=3.11 (when the `venv` module is missing,
#      print the OS-specific remediation and exit 2 — no auto-sudo,
#      no system-package mutation)
#   2. create an isolated venv under $HOME/.digital-twins/.venv —
#      via `uv venv` when the `uv` binary is on PATH (preferred: faster,
#      no system-python dependency), else the stdlib `venv` module
#   3. pip install "digital-twins-kb[<extras>]" from PyPI
#      (`uv pip install` when the venv was created by uv, else the
#      venv's own pip)
#   4. run `digital-twins setup` (the first-run wizard)
#   5. optionally run the first ingest
#
# This is the *remote* flavour of scripts/install-local.sh (which is what a
# git-checkout user runs directly). Both share the same conventions:
#   - Host-neutral (NFR-13): no host paths, usernames, or interpreter pins;
#     $HOME and the Python >=3.11 floor are the only "pins".
#   - All external command invocations route through run_cmd() so the
#     INSTALL_SH_EXEC env can intercept them in tests (same BOOTSTRAP_EXEC /
#     UNINSTALL_EXEC pattern).
#
# Security: run `bash -x scripts/install.sh` first to audit every command.
# The script performs no network access except `pip install` (PyPI).
#
# Exit codes:
#   0  success (install + setup completed)
#   1  a step failed (remediation printed)
#   2  no usable Python >=3.11 was found
#   3  `digital-twins setup` reported a failing health check
#   5  `digital-twins setup` could not resolve cloud endpoints
#   6  `digital-twins setup` was interrupted (Ctrl-C / EOF at a prompt)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/terrygzhou/digital-twins/main/scripts/install.sh | bash
#   # or, with options, download first then run:
#   curl -fsSL https://raw.githubusercontent.com/terrygzhou/digital-twins/main/scripts/install.sh -o install.sh && bash install.sh [options]
#
#   Options:
#     --extras LIST       Comma-separated extras (default: mcp). "" for base.
#     --cloud             Pass --cloud to `setup` (force cloud backend mode)
#     --cloud-env         Pass --cloud-env to `setup` (non-interactive cloud
#                         mode: endpoints come from the KB_* env vars, never
#                         a prompt; exit 5 names the missing var(s))
#     --skip-services     Pass --skip-services to `setup`
#     --run-ingest        After setup, run `digital-twins run --source fs`
#     --no-setup          Stop after the pip install; do not run the wizard
#     --python PATH       Use this specific python interpreter
#     --dist PATH|NAME    Install this wheel/directory instead of the PyPI
#                         dist (a local checkout or a local .whl).
#     --help              Show this help.
#
# Environment overrides:
#   INSTALL_SH_EXEC      Fake binary that intercepts run_cmd() calls (tests).
#   INSTALL_SH_EXEC_LOG  Where the fake writes its JSONL argv log.
#   INSTALL_SH_DIST      Override the PyPI dist name (default: digital-twins-kb).
#   INSTALL_SH_CLI       Override the console-script name (default: digital-twins).
set -euo pipefail

# All external commands route through run_cmd() so a test harness can set
# INSTALL_SH_EXEC to a fake binary that logs argv and returns canned output.
run_cmd() {
  if [ -n "${INSTALL_SH_EXEC:-}" ]; then
    "$INSTALL_SH_EXEC" "$@"
  else
    "$@"
  fi
}

# --- constants ---------------------------------------------------------------
DIST_NAME="${INSTALL_SH_DIST:-digital-twins-kb}"
# The pip distribution name vs. the console-script name.  The
# dist publishes as digital-twins-kb on PyPI (bare digital-twins is
# name-blocked); the CLI it installs is still `digital-twins`.
CLI_NAME="${INSTALL_SH_CLI:-digital-twins}"
MIN_PY_MINOR="11"

# --- flags -------------------------------------------------------------------
EXTRAS="mcp"
DIST_OVERRIDE=""
CLOUD=0
CLOUD_ENV=0
SKIP_SERVICES=0
RUN_INGEST=0
NO_SETUP=0
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

# Find a usable python: explicit override, then python3, then python, then
# common absolute locations.
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

# Ensure the `venv` module is importable from $1. On PEP 668 distros where
# the venv module is missing, we *do not* auto-sudo; we print the exact
# remediation and exit 2 (the user installs python3-venv themselves).
ensure_venv_module() {
  local interp="$1"
  if run_cmd "$interp" -c 'import venv' 2>/dev/null; then
    return 0
  fi
  note "'venv' module is missing for $interp."
  note "On Debian/Ubuntu install it with:  sudo apt install python3-venv"
  note "On Fedora/RHEL:  sudo dnf install python3-libs   (then re-run)"
  note "On macOS (Homebrew python): the venv module ships with it — check your PATH."
  return 1
}

# --- main --------------------------------------------------------------------
main() {
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
    if [ -z "$PYTHON_OVERRIDE" ]; then
      note "no Python >= 3.${MIN_PY_MINOR} found on PATH."
      note "Install one (your OS package manager's python3) and re-run."
    fi
    exit 2
  fi
  note "using python: $py ($(py_version "$py"))"

  # --- 2) create an isolated venv ------------------------------------------
  # Prefer uv when it is on PATH (host-neutral: we never install uv
  # itself — we only use it when the user already has it); fall back to
  # the stdlib venv module (which may still be missing on PEP 668
  # distros — printed remediation + exit 2, no auto-sudo).
  local use_uv=0
  # Detect uv through run_cmd so the mocked-exec test harness (empty
  # PATH) can steer the branch:
  #   FAKE_UV_PRESENT=<existing file> -> force uv on (probe skipped).
  #   FAKE_UV_PRESENT=<missing file>   -> force uv off (probe skipped).
  #   unset                            -> real `command -v uv` probe,
  #                                       still routed through run_cmd
  #                                       so it is logged/interceptable.
  if [ -n "${FAKE_UV_PRESENT:-}" ] && [ -e "${FAKE_UV_PRESENT}" ]; then
    use_uv=1
  elif [ -z "${FAKE_UV_PRESENT:-}" ] && run_cmd command -v uv >/dev/null 2>&1; then
    use_uv=1
  fi
  # Only probe the venv module when uv will NOT create the venv — the
  # uv path works regardless of the stdlib venv module (uv manages its
  # own environment), so probing it there is wasted work (and would
  # confuse the mocked-exec test log).
  local venv_module_ok=1
  if [ "$use_uv" -eq 0 ]; then
    if [ -n "${FAKE_VENV_MISSING:-}" ] && [ -e "${FAKE_VENV_MISSING}" ]; then
      venv_module_ok=0
    elif ! run_cmd "$py" -c 'import venv' 2>/dev/null; then
      venv_module_ok=0
    fi
  fi
  if [ "$use_uv" -eq 0 ] && [ "$venv_module_ok" -eq 0 ]; then
    note "'venv' module is missing for $py (and uv is not available)."
    note "On Debian/Ubuntu install it with:  sudo apt install python3-venv"
    note "On Fedora/RHEL:  sudo dnf install python3-libs   (then re-run)"
    note "On macOS (Homebrew python): the venv module ships with it - check your PATH."
    note "Or install uv (https://docs.astral.sh/uv/) and re-run."
    exit 2
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
  # pip_cmd: the installer for the venv — the venv's own pip, or uv
  # (targeted at the venv's python) when uv created it.
  # pip_cmd: the installer for the venv.  The stdlib-venv path uses the
  # venv's own pip (`<venv>/bin/pip install …`); the uv path uses
  # `uv pip install --python <venv python>` (uv-managed venvs ship no
  # pip, and the --python flag targets the venv, not the system one).
  local pip_cmd=("$venv_pip" install)
  if [ "$use_uv" -eq 1 ]; then
    pip_cmd=(uv pip install --python "$venv_dir/bin/python")
  fi

  # --- 3) pip install from PyPI --------------------------------------------
  local extra_spec
  if [ -n "$DIST_OVERRIDE" ]; then
    extra_spec="$DIST_OVERRIDE"
  elif [ -n "$EXTRAS" ]; then
    extra_spec="${DIST_NAME}[${EXTRAS}]"
  else
    extra_spec="${DIST_NAME}"
  fi
  note "installing $extra_spec from PyPI (this can take a while) ..."
  # --upgrade keeps a re-run current; --quiet keeps the output tidy.
  if ! run_cmd "${pip_cmd[@]}" --upgrade --quiet "$extra_spec"; then
    fail "install failed. Check your network / PyPI access and re-run."
  fi

  # --- 3b) verify the CLI is current ----------------------------------------
  # A re-run on a host with a *stale* venv can leave the installer a
  # silent no-op (requirement already satisfied), so the very next
  # '$venv_bin setup' fails with "No such command 'setup'". Detect that
  # here: report the installed version, and force a reinstall when the
  # CLI predates the 'setup' subcommand. Pure-bash string ops only (no
  # head/grep, so the mocked-exec tests with an empty PATH still exercise
  # every branch).
  local cli_version cli_help
  cli_version="$(run_cmd "$venv_bin" --version 2>/dev/null || true)"
  note "installed version: ${cli_version:-unknown}"
  cli_help="$(run_cmd "$venv_bin" --help 2>/dev/null || true)"
  case "$cli_help" in
    *setup*) ;;  # current CLI: 'setup' present in help
    *)
      note "stale CLI detected (no 'setup' subcommand); forcing a reinstall."
      if ! run_cmd "${pip_cmd[@]}" --upgrade --force-reinstall --quiet "$extra_spec"; then
        fail "forced reinstall failed. Check your network / PyPI access and re-run."
      fi
      note "reinstalled $extra_spec."
      ;;
  esac

  # --- 4) run the setup wizard ---------------------------------------------
  local setup_rc=0
  if [ "$NO_SETUP" -eq 0 ]; then
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
      1) note "setup: a health check failed (see the report above); re-run '$venv_bin setup' after fixing the endpoint." ;;
      3) note "setup: the local Docker stack did not become healthy; re-run after it is up." ;;
      5) note "setup: cloud endpoints could not be resolved (see which KB_* var is empty above)." ;;
      6) note "setup: the wizard was interrupted before the backend was configured; re-run '$venv_bin setup' (or set the KB_* env vars) to continue." ;;
      *) fail "setup exited unexpectedly ($setup_rc)." ;;
    esac
    if [ "$setup_rc" -eq 1 ] || [ "$setup_rc" -eq 3 ] || \
       [ "$setup_rc" -eq 5 ] || [ "$setup_rc" -eq 6 ]; then
      exit "$setup_rc"
    fi
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
