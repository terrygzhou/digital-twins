# Tasks: 019b — Issue 6 (supersede-after-extract + targeted heal)

## 1. Unit tests (RED-first)
- [x] 1.1 (landed in tests/unit/test_supersede_order.py::test_failed_extract_mutes_no_graph_state; plus T3 success path and a concurrency-isolation variant in the same file) T1 `tests/unit/test_pipeline_extraction.py`:
      `extraction_failure_leaves_prior_edges_live` — pre-populate
      StubNeo4j with the item's live MENTIONED/REL (via a prior
      successful `materialize` call), stub `extract` to raise for item A,
      run `_run_extraction`; assert:
        - no `SET m.valid_to` / supersede Cypher executed for item A
        - no `materialize` for item A
        - prior MENTIONED/REL Cypher still present (edges live)
        - `audit_items` row `failed` for item A (db-backed test)
      **Verification:** fails on current code (supersede-first).
- [x] 1.2 (tests/unit/test_extraction_heal.py: heal_reextracts_only_latest_failed, still_failed_records_new_failed_row, skips_locked_source, no_content_in_qdrant_counts_failed, no-op gate-off/no-driver/no-pending) T2 `tests/unit/test_extraction_heal.py` (new):
      `heal_reextracts_only_failed_items` — seed `audit_items` with two
      `failed` rows (item_a, item_b) + one `ok`; stub extract to succeed;
      run `heal_extraction`; assert extract called exactly for
      {a, b}, `ok` rows written for both, `ok` audit row not re-healed.
      `heal_skips_locked_source` — hold the ingest lock for the source;
      heal skips that source's items (no extract calls for it).
      **Verification:** fails before implementation.
- [x] 1.3 T3: existing `test_extraction_runs_when_gate_on...` +
      `test_extraction_isolated_per_item_on_failure` stay green
      (success path still supersedes then materializes).

## 2. Implementation
- [x] 2.1 (+ success-path `ok` audit row so the ledger reflects per-item outcomes) `pipeline.py::_run_extraction.process_item`: reorder to
      `extract → supersede → materialize`; failure path unchanged
      (warn + `record_audit_item`), now with zero graph mutation.
- [x] 2.2 `pipeline.py::heal_extraction(cfg, db, qdrant, neo4j,
      run_id)`: latest-failed-per-item from `audit_items`; per item read
      content from Qdrant payload (`full_content` else `text`, chunk 0);
      run the same extract→supersede→materialize block (serial; reuse
      `process_item`'s ordering); `record_audit_item` ok/failed; skip a
      source whose `ingest_locks` row is live.
- [x] 2.3 (+ registry tool schema; body gates on `trigger_run` internally like kb_ingest) `mcp/dispatch.py`: `kb_extraction_heal` tool body — role-gated
      like `kb_ingest`, calls `heal_extraction`, returns
      `{"ok": true, "healed": N, "failed": M}`. Register in the tool map
      + role table.
- [x] 2.4 `entities.py` docstring: correct the module-level note
      ("supersede … before materialization" → after extract).

## 3. Guards
- [x] 3.1 `pytest tests/unit/test_knob_docs.py tests/integration/test_portability.py -q`
      green.
- [x] 3.2 (full unit suite: 921 passed; the 63 socket/network failures are the pre-existing sandbox baseline, not regressions) Full unit suite green (no regression; defaults unchanged —
      heal is a separate trigger path).
- [x] 3.3 NFR-1 top check unchanged (one point per content, every path).

## 4. Semver + release note
- [x] 4.1 CHANGELOG: "graph extraction is idempotent on failure
      (supersede-after-extract); targeted heal tool kb_extraction_heal
      converges stranded edges from failed extractions."

## 5. Bookkeeping
- [x] 5.1 SDD ledger `.superpowers/sdd/019-ingest-reliability/progress.md`:
      append 019b entry (RED/GREEN/validation).
- [x] 5.2 `docs/ingest-issue-6-supersede-edge-loss.md`: status → FIXED,
      link to this spec/plan.
