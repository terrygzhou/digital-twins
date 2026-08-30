# Tasks: 007 T024 deferred-fix slice (D-007-1..3)

**Input**: `specs/007-t024-followup/plan.md`; findings record at
`.superpowers/sdd/deferred-minors/007-t024-findings.md`.

**Tests**: included — constitution III (Test-First, NON-NEGOTIABLE):
RED commit failing before the GREEN commit, per task.

## Task 1 — D-007-1: env-hermetic MCP tests

- [x] T1 RED: convert `_service_token_for(db)` in
  `tests/unit/test_mcp_config_plumbing.py` to take `monkeypatch` and use
  `monkeypatch.setenv`; convert `test_sc005_transport_parity` in
  `tests/integration/test_mcp_integration.py` to `monkeypatch.delenv("DT_SERVICE_TOKEN",
  raising=False)` + `monkeypatch.setenv("DT_PERSONAL_TOKEN", bob_token)`
  (drop the raw os.environ + try/finally restore). Record the pre-fix
  ordered-run failure as the RED line. Verify: ordered run
  `pytest tests/unit/test_mcp_config_plumbing.py tests/integration/test_mcp_integration.py -q`
  green.

## Task 2 — D-007-2: keyed + locked qdrant client cache

- [x] T2 RED: new `tests/unit/test_mcp_qdrant_cache.py` — one construction
  per distinct config key; two configs differing only in an api-key-ish
  knob → two distinct clients; same config → same cached object.
  Verify: RED (cache is still flat/unkeyed).
- [x] T2 GREEN: `digital_twins/mcp/dispatch.py` — key
  `_qdrant_client_cache` on the hashable tuple of config-derived
  connection params; guard check-then-set with a `threading.Lock`.
  Verify: `pytest tests/unit/test_mcp_qdrant_cache.py
  tests/unit/test_mcp_kb_search.py tests/unit/test_mcp_kb_ingest.py
  tests/integration/test_mcp_kb_tools.py -q` green.

## Task 3 — D-007-3: nested default merge for kb_ingest

- [ ] T3 RED: test that `ctx.config = {"chunking": {"max_chars": 999}}`
  yields a merged config with the override AND the sibling `chunking.*`
  schema defaults in the `run_pipeline` call. Verify: RED (flat merge drops
  siblings).
- [ ] T3 GREEN: `dispatch.py` — add `_deep_merge(base, override)`
  (006 semantics, local to dispatch — no web/ import); use for `merged_cfg`
  in `_kb_ingest_body`. Verify: `pytest tests/unit/test_mcp_kb_ingest.py
  tests/integration/test_mcp_kb_tools.py -q` green.

## Verify & close

- [ ] V1 full suite green (baseline 831 + new tests); standing guards
  (`test_portability.py`, `test_knob_docs.py`) green; ordered flake run
  green ×2; no version bump; diff touches only the planned files.
- [ ] V2 whole-branch review (separate subagent) recorded in the ledger.
- [ ] V3 STOP at merge gate — ask owner; do NOT merge without go-ahead.

## Notes

- No changes to `digital_twins/web/`, `ingest/`, `health.py`,
  `accounts.py`, `config/`. No new knobs. Version stays `0.7.0`. No
  CHANGELOG entry for a defect slice (record as a ruling).
- Test runner: `/home/terry/projects/digital-twins/.venv/bin/python -m
  pytest` from the worktree root.
