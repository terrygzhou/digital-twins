# Feature 007 — SDD progress (per-task detailed record)

Branch `007-kb-search-chat` (worktree `.worktrees/007-kb-search-chat`).
Phases 1–3 complete in earlier spans; Phase 4 (T018–T022) recorded below.

## Phase 4

- **T018** (no-op confirm): `grep -c "_stub_schema"
  digital_twins/mcp/registry.py` → `0` (no remaining callers; the helper was
  deleted in an earlier span).
- **T018b** (2 commits):
  - `3aca3b8` — delete `tests/unit/test_mcp_stubs.py` (the four BR-10 stubs no
    longer exist; their stub-shape tests are superseded by the real bodies).
  - `b3d9528` — `test_sc001_fresh_client_gets_full_tool_list` in
    `tests/integration/test_mcp_integration.py`: the 4 KB tools no longer
    assert `not_implemented_yet`; new assertion — each KB tool's `error.code`
    (if any) is NOT `not_implemented_yet` and the observed code set ⊆
    `{bad_request, not_implemented, permission_denied, run_failed,
    qdrant_unavailable, embedding_unavailable, config_not_loaded,
    source_disabled, prerequisite_missing, unknown_source}`. All 6 tests in
    the file green.
- **T019** — `039d7b7`: `tests/integration/test_mcp_kb_tools.py` (6 tests):
  (a) service-token → stdio → `kb_search` vs fake Qdrant (owner-scoped
  filter asserted; second owner's point never appears); (b) personal-token →
  stdio → `kb_ingest` vs fake `run_pipeline` → `audit_runs` row with
  `trigger="mcp"`, `scheduled_by=<caller>`, `agent_kind` in
  `per_source_counts`; (c) `kb_health` → per-endpoint check list
  (`["qdrant","neo4j","llm"]`, exact field set, all `ok=False` unconfigured);
  (d) both transports populate `MCPContext.config` (stdio `serve` + http
  handler on `127.0.0.1:0`), and `MCPContext(config=None)` fails closed with
  `config_not_loaded`. `pytest tests/integration/test_mcp_kb_tools.py` → 6
  passed.
- **T020** — `pytest tests/integration/test_portability.py` → **3 passed**
  (portability guard green; 007 adds zero shipped files, SHIPPED lists
  untouched).
- **T021** — `pytest tests/unit/test_knob_docs.py` → **20 passed** (knob-docs
  guard green; zero knob changes).
- **T022** — full suite `pytest` on the worktree → **831 passed** in 60.79s
  (0 failed, 0 skipped; SC-008 met: baseline 762 at main @ 7fd350b + 007's
  ~70 new tests). Version stays `0.6.0` until T023 (R8/R15).
