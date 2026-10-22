# Plan: 019b — Issue 6 (supersede-after-extract + targeted heal)

## Summary

Reorder the per-item extraction block from
`supersede → extract → materialize` to
`extract → supersede → materialize` so a failed LLM call mutates no graph
state (prior live edges stay live; idempotent-by-default). Then add a
targeted `kb_extraction_heal` tool body that re-extracts only items whose
`audit_items` row is `failed` (latest per item_key), converging the graph
without a full re-walk. No schema change, no new knobs.

## Constitution check

- **I. Portability**: no host values; the heal path reuses the config
  layer (`llm.*`, `extraction.*`, `state.db` path). No new config.
- **II. Deterministic, Idempotent Ingestion**: `supersede`/`materialize`
  remain idempotent MERGEs; the reorder changes *when* they run, never
  edge identity. NFR-1 parity for the graph layer now holds on the
  failure path too.
- **V. Test-First (NON-NEGOTIABLE)**: T1 (failed-extract leaves edges
  live + supersede not executed for that item) lands RED first and is the
  acceptance gate; T2 (heal) and T3 (success path unchanged) complete the
  suite before implementation.

## Files touched

- `digital_twins/ingest/pipeline.py`
  1. `_run_extraction.process_item`: reorder to
     `extract → supersede → materialize`; keep per-item isolation
     (`except` → warn + `record_audit_item`, no graph mutation on
     failure).
  2. New `heal_extraction(cfg, db, qdrant, neo4j, ...)`:
     - read `audit_items` for `status='failed'` (latest per
       (source, item_key));
     - for each: read the item's content (Qdrant payload
       `full_content`/`text` for the item's chunks, joined per
       item_key, or re-walk the source's `read()` filtered to the key —
       decision: **source `read()` filtered** is not available per-key,
       so read the Qdrant payload `full_content` for the item's first
       chunk and reuse it; fall back to `text`), then run the same
       `extract → supersede → materialize` block;
     - write `audit_items` rows (`ok`/`failed`) under a new run_id with
       `trigger='heal'`;
     - respect `ingest_locks` (skip a source while ingest holds the
       lock); gate on `extraction.enabled` + neo4j presence like
       `_run_extraction`.
- `digital_twins/mcp/dispatch.py`
  - new `kb_extraction_heal` tool body (role-gated like
    `kb_ingest`; owner-scope unchanged), wiring `heal_extraction`.
- `digital_twins/ingest/entities.py`
  - nothing changes to `supersede`/`materialize`; the module docstring
    note ("supersede … before") is corrected to reflect the new
    ordering.
- `tests/unit/test_pipeline_extraction.py` (extend, T1 + T3)
- `tests/unit/test_extraction_heal.py` (new, T2)
- `docs/ingest-issue-6-supersede-edge-loss.md` — status OPEN → FIXED
  pointer to this spec.

## Test-first sequence (TDD)

1. RED: T1 in `test_pipeline_extraction.py` — failed extract leaves live
   edges (assert `supersede` Cypher absent for that item; materialize
   absent; audit_items row present).
2. RED: T2 in `test_extraction_heal.py` — heal reads failed audit
   rows, extracts exactly those items, writes ok/failed.
3. GREEN: implement FR-1 reorder + `heal_extraction` + `kb_extraction_heal`
   body.
4. GREEN: T3 success path unchanged (existing tests stay green).
5. Guards: `test_knob_docs.py` + `test_portability.py` + full unit suite.

## Rollout

- No migration (no DDL). Deploy with 019's v4 schema (already on the box).
- OCI: after restart, run `kb_extraction_heal` once to converge the
  stranded edges from the two live `timed out` extractions.
