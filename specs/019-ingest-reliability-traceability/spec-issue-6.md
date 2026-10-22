# Feature 019b: Issue 6 — idempotent-by-default graph extraction (supersede-after-extract, targeted heal)

Status: SPEC (clarified) · 2026-10-22 · parent: `specs/019-ingest-reliability-traceability/spec.md`
Source diagnostic: `docs/ingest-issue-6-supersede-edge-loss.md` (issues 1–5 doc, §6).

## Background

`_run_extraction` (`pipeline.py`) runs `supersede → extract → materialize`
per item. `supersede` soft-deletes the item's **live** `MENTIONED`/`REL`
edges **before** `extract`. If `extract` then fails (LLM timeout /
non-JSON — observed live in the dt-mcp journal), the `except` isolates the
failure and `materialize` never runs: the item's graph edges are left
soft-deleted with no replacement. Re-ingest is therefore **lossy, not
idempotent**, for the graph layer (Qdrant is idempotent via
`ids.point_id_s4`; Neo4j is not on extraction failure).

Consequences: silent graph degradation on any flaky LLM call, non-
convergent re-ingest (violates the spirit of NFR-1 parity for the graph
layer), and a traceability blind spot (no per-item record of which items
lost edges — Issue 4's `audit_items` records the failure but nothing
repairs the graph).

## User Scenarios & Testing

**S1 — failed extraction preserves live edges (idempotent-by-default).**

1. An item with live `MENTIONED`/`REL` edges is re-ingested.
2. The LLM extraction call fails (timeout / 5xx after retry / non-JSON).
3. After the run, the item's pre-existing live edges are **still live**
   (`valid_to IS NULL`); no state was mutated by the failed attempt.
   The failure is logged and recorded in `audit_items` (status `failed`).

**S2 — successful extraction supersedes then materializes.**

1. The LLM extraction call succeeds.
2. Only then are the item's prior live edges invalidated
   (`supersede`) and the new extraction's edges issued (`materialize`).
   Net effect on success is identical to today's behavior.

**S3 — targeted heal of stranded failures.**

1. After a run (or sweep), `audit_items` holds `failed` rows for items
   whose extraction failed.
2. A heal pass (`kb_extraction_heal`) re-runs extraction **only** for
   those failed items (`audit_items.status='failed'`, last-error per
   item), reusing each item's recorded source/keys.
3. Successful heals mark the item `ok` in `audit_items`; terminal
   failures stay `failed` (and, by S1's ordering, leave the prior live
   edges intact).

**Edge cases**

- **Concurrent heal (issue 5-A):** heal re-enters the pipeline's
  per-source `ingest_locks` discipline; an in-flight ingest holds the
  lock → heal for that source is skipped (not blocked).
- **No content:** heal only re-extracts; the item's Qdrant point and
  `SourceItem` node already exist from the original ingest. If an item's
  content is gone, heal records `failed` again — no crash.
- **Dry-run / gate-off:** `extraction.enabled=false` or no Neo4j driver →
  heal is a no-op (mirrors `_run_extraction` gating).
- **Determinism (NFR-1):** the reorder changes *when* `supersede` runs,
  never point identity or edge identity; `materialize`/`supersede`
  remain idempotent MERGEs.

## Functional Requirements

- **FR-1.** `supersede(item)` MUST run **after** a **successful**
  `extract(item)`, immediately before `materialize`. A failed extract
  MUST mutate no graph state (prior live edges remain `valid_to IS NULL`).
- **FR-2.** On terminal extraction failure, the pipeline MUST record
  `audit_items` (already wired, issue 4-A) and MUST NOT strand prior
  live edges.
- **FR-3.** A `kb_extraction_heal` surface (MCP tool body in
  `mcp/dispatch.py`, no schema change) reads failed `audit_items` rows
  (latest `failed` per item_key, all sources), re-runs
  `extract → supersede → materialize` for each, under the source
  `ingest_locks` lock, and writes `audit_items` `ok`/`failed` rows for
  the heal attempt (new run_id, `trigger='heal'`).
- **FR-4.** No new config knobs. No schema change. No change to point
  identity, highwater, dedup, or sub-batch behavior.

## Acceptance

- T1 (RED-first, per constitution): unit test stubs `extract` to raise
  for one item; asserts the item's pre-existing MENTIONED/REL edges are
  still live (`valid_to IS NULL`) after the failed extraction, and that
  `supersede`'s Cypher did **not** execute for that item. Fails on
  current code (supersede-first soft-deletes the edges), passes with
  FR-1.
- T2: heal unit test — `audit_items` with two `failed` rows → heal calls
  `extract` exactly for those items, writes `ok` rows on success, `ok`/
  `failed` per item; lock-held source is skipped.
- T3: success-path test — successful extract still supersedes then
  materializes (existing extraction tests stay green unchanged).
- Guards: `test_knob_docs.py`, `test_portability.py`, full unit suite
  green.

## Out of scope

- Full-corpus re-walk heal (heal is targeted via `audit_items` only).
- 3-strike alerting (issue 4-C), HNSW refresh hook (issue 3-B),
  per-run highwater (issue 1-B).
- Changing soft-delete (`valid_to`) semantics — the fix is *when* to
  soft-delete, not *whether* edges can be invalidated.
