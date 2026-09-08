# 014 — tasks (spec-lite)

- [x] T1: RED — six cloud-mode tests in `tests/unit/test_bootstrap_script.py`
      (+ `_run_script` gains `args`/`stdin` params).
- [x] T2: GREEN — `--cloud` mode in `scripts/bootstrap-local.sh`
      (cloud_bootstrap(), EXIT_CLOUD=5, arg parsing, help text).
- [x] T3: contract sync — `specs/008-service-hosting/contracts/bootstrap-cli.md`
      (usage, env table, exit 5, cloud behavior).
- [x] V1: full suite green (see progress.md); commit f2820a5.
