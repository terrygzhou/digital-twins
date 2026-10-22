# Plan: 019 — Ingest reliability (resumable sub-batched pipeline, concurrent retried extraction)

## Summary
Three defects in `digital_twins/ingest/pipeline.py` + `entities.py`,
observed live on the OCI box (doc:
`docs/ingest-reliability-traceability.md`, issues 1/2/3):
(a) highwater written only at source-end → a never-completing 15k
batch means the cursor never advances → every run re-walks the whole
tree; (b) extraction is one blocking un-retried LLM call per item,
serially → day-scale ETA; (c) the whole batch commits atomically at
the end → no progress signal, total loss on crash.
This feature: sub-batches the pipeline (embed → upsert → graph →
highwater-checkpoint per sub-batch), adds a bounded thread pool for
extraction, and adds 5xx retry-once to the extraction LLM transport.

## Constitution check
- **I. Portability**: the two new knobs carry portable defaults (1000
  / 1); no host value in code, config defaults, or docs (the OCI
  `max_items=15000` override stays on the box).
- **II. Deterministic, Idempotent Ingestion**: point IDs unchanged
  (`ids.point_id_s4`); sub-batch ordering is the source's deterministic
  read order; `materialize`/`supersede` remain idempotent MERGEs; the
  highwater checkpoint is a skip-optimization, never the dedup
  authority. Same content → one point, across every trigger path
  (NFR-1, NFR-14) — the top acceptance check still applies.
- **Test-First (V)**: tests land before the pipeline change; existing
  pipeline tests (single-batch runs) must stay green unchanged.

## Files touched
- `digital_twins/config/knobs.py` — add `pipeline.sub_batch_size`
  (int, default 1000; 0 = legacy no-sub-batching) and
  `extraction.concurrency` (int, default 1).
- `digital_twins/config.example.yml` — document both knobs (knob-doc
  sync guard moves with `knobs.py`).
- `digital_twins/ingest/pipeline.py`:
  1. Read `pipeline.sub_batch_size` (0 → whole list is one
     sub-batch, i.e. legacy path).
  2. Replace the single `embed → upsert → graph → extract →
     upsert_highwater` block with a loop over sub-batches: per
     sub-batch embed that sub-batch's chunks, Qdrant upsert (wait),
     graph upsert (if neo4j), extraction fan-out, then
     `upsert_highwater` for every item in the sub-batch.
     `total_points`, `counts` and the audit row keep their current
     end-of-source semantics.
  3. Pass `extraction.concurrency` into `_run_extraction`; inside it,
     run the per-item `supersede → extract → materialize` block in a
     `ThreadPoolExecutor(max_workers=concurrency)` (concurrency=1
     → serial, today's behavior); per-item failure isolation
     unchanged (warning log, continue).
- `digital_twins/ingest/entities.py`:
  1. The raw urllib LLM call (transport at `entities.py:204-208`):
     add 5xx retry-once (house style, mirror `llm.py:74-77` /
     `embedding.py:156-160`). Timeouts: no retry.
  2. Extract the per-item block into a module-level helper so the
     pipeline's worker and the serial path share one code path.
- `tests/unit/test_pipeline.py` (or a new
  `tests/unit/test_pipeline_subbatch.py`): fake source with
  N > sub_batch_size; assert per-sub-batch call order (embed →
  upsert → graph → highwater) and that highwater rows appear after
  each sub-batch, not only at source end.
- `tests/unit/test_pipeline_extraction.py`: fake transport;
  `extraction.concurrency=4` vs `=1` produce identical materialized
  entity sets; 5xx-then-200 succeeds (retried); persistent 5xx →
  logged failure, remaining items proceed; timeout → no retry.
- `tests/unit/test_knob_docs.py`: stays green by construction (knobs
  + example moved together).

## Semver
MINOR. Additive knobs with legacy-preserving defaults; no schema
change; no public API change (`run_pipeline` signature unchanged —
the knobs are read from `cfg` inside).

## Risks / mitigations
- **Sub-batch highwater ordering**: highwater is upserted per item in
  read order inside the sub-batch; the `_cursor` `MAX(last_key)` read
  is unchanged, so the final cursor value equals today's source-end
  value (clarification Q1: no cursor-type redesign in 019).
- **Thread pool + sqlite**: highwater upserts happen on the main
  thread *after* the pool joins — no sqlite handle is shared across
  threads.
- **Neo4j session usage under threads**: `materialize`/`supersede`
  open their sessions per call (verified in `entities.py`), so the
  pool needs no shared session; the `neo4j` driver argument is only
  passed through.
