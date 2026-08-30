#!/usr/bin/env bash
# 008 T033 (US3): local Docker bootstrap — one invocation on a clean host.
# Staged: this is the T030–T032 RED stub; T033 replaces the body.
# Contract pinned by tests/unit/test_bootstrap_script.py (R1): all external
# command invocations MUST route through run_cmd() so BOOTSTRAP_EXEC can
# intercept them in tests.
set -euo pipefail

run_cmd() {
  if [ -n "${BOOTSTRAP_EXEC:-}" ]; then
    "$BOOTSTRAP_EXEC" "$@"
  else
    "$@"
  fi
}

main() {
  echo "bootstrap-local.sh: not implemented yet (008 T033)" >&2
  return 1
}

main "$@"
