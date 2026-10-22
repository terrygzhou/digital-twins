# Feature 019: Ingest reliability — resumable sub-batched pipeline, concurrent LLM extraction

## Background

The live OCI ingest run (`dt-mcp.service`, `/data/keyva` fs source,
`max_items=15000`) revealed three compounding reliability defects in
`digital_twins/ingest/pipeline.py` + `digital_twins/ingest/entities.py`
(diagnostic: `docs/ingest-reliability-traceability.md`, issues 1/2/3,
options 1-A, 2-A/B, 3-A):

1. **No incremental resume.** `highwater` is written only at
   source-end, after the whole 15,000-item batch is embedded + graphed +
   extracted. No 15,000-item pass has ever completed, so
   `highwater.source='fs'` has 0 rows and every run re-reads,
   re-chunks, and re-embeds the entire tree.

2. **Serial, un-retried LLM extraction.** `_run_extraction` makes one
   blocking `urllib` LLM call per item, sequentially, with no 5xx retry
   (unlike `embedding.py` / `llm.py`, which retry-once on 5xx). On a
   15,000-item pass this is a day-scale ETA, and transient LLM failures
   are log-only — no retry, no record.

3. **Atomic end-of-batch commit with no progress signal.** Qdrant upsert,
   graph upsert, and extraction all happen once, after the entire batch
   is embedded — for the whole duration of a run, the stores show no
   progress, and a crash loses the entire pass.

Additionally, the LLM extraction call (`entities.extract`, `entities.py`
transport at `entities.py:204-208`) has no retry/backoff: a single
5xx/timeout kills that item's extraction, logged and forgotten.

## User Scenarios & Testing

**Primary scenario (resume after crash)**

1. A run processes the fs source in sub-batches (default 1000 items,
   configurable via `pipeline.sub_batch_size`).
2. After each sub-batch's Qdrant upsert + graph upsert commit, the
   highwater cursor for that source advances to the last item key in the
   sub-batch (checkpoint, not source-end).
3. The process dies mid-run (OOM, service restart, operator stop).
4. The next run starts from the persisted highwater cursor: items up to
   and including the last committed sub-batch are skipped; at most one
   sub-batch of work is redone.

**Concurrent extraction scenario**

1. `extraction.concurrency` is set (new int knob, default 1 = today's
   behavior).
2. During a run, the `supersede → extract → materialize` block for each
   item is fanned out across up to `extraction.concurrency` workers
   (thread pool), instead of one blocking call per item.
3. Each worker's LLM call retries once on 5xx (house style, mirroring
   `llm.py` / `embedding.py` retry-once) before surfacing the failure.
4. Per-item failures remain isolated: one failed item does not stop the
   remaining items (existing isolation contract), and the failure is
   still logged (per-item failure records are feature 020, not here).

**Progress-visibility scenario**

1. During a long run, the per-sub-batch commit produces N intermediate
   visible states (Qdrant count, graph counts, highwater) instead of
   one final state.

**Edge cases**

- **Idempotency (NFR-1)**: sub-batching must not change point identity —
  point IDs remain content-addressed (`ids.point_id_s4`); re-running a
  sub-batch upserts the same points. Highwater checkpointing is a
  skip-optimization, never the dedup authority.
- **Default-off behavior**: `extraction.concurrency` default is 1;
  `pipeline.sub_batch_size` default must keep existing tests green and
  must not change observable behavior for runs under the cap (a
  single-sub-batch run behaves exactly as today).
- **Dry-run**: `dry_run=True` skips commits (including the highwater
  checkpoint and graph/extraction writes) exactly as today; sub-batching
  must not break the dry-run point-count path.
- **`max_items` cap**: the cap (`sources.<name>.max_items`, default 200)
  bounds the *items* read; sub-batching partitions those items — the two
  are independent.
- **Neo4j-only failure mid-sub-batch**: if graph upsert fails for a
  sub-batch, the run fails (audit row `failed`) exactly as today;
  highwater advances only after the sub-batch's graph commit succeeds,
  so a crash resumes from the last fully-committed sub-batch.
- **Extraction disabled** (`extraction.enabled: false` or no Neo4j): the
  concurrency knob is a no-op; the pipeline behaves as today.

**Key test scenarios**

- Sub-batch commit order: Qdrant upsert → graph upsert → highwater
  checkpoint, per sub-batch (assert ordering in the pipeline with a fake
  source of N > sub_batch_size items).
- Crash-resume: after a simulated failure at sub-batch k, the next run's
  `since` cursor equals the last committed item key; items ≤ cursor are
  never re-read by the source.
- Concurrency: with a fake LLM transport, `extraction.concurrency=4`
  produces the same set of materialized entities as `=1` (deterministic
  MERGEs), with wall-clock reduction when the transport is the bottleneck.
- Retry: a fake 5xx-then-200 transport yields success with
  `extraction.concurrency=1` (one retry); a fake persistent-5xx transport
  fails the item (log only, run continues) — matching existing
  log-only isolation.
- Knob-doc sync: `tests/unit/test_knob_docs.py` green (new knob in
  `config/knobs.py` + `config.example.yml` together).
- Existing 001–018 pipeline tests stay green (no behavior change at
  defaults; highwater now written per sub-batch — for a single-sub-batch
  run, the final value is identical to the source-end value).

## Functional Requirements

- **FR-1**: The pipeline partitions each source's item list into
  sub-batches of `pipeline.sub_batch_size` items (new int knob, default
  1000; `0` or unset = no sub-batching, i.e. legacy single-batch
  behavior) and, per sub-batch: embeds, upserts to Qdrant, upserts the
  graph (when Neo4j is configured), then checkpoints the highwater
  cursor for the source to the sub-batch's last item key.
- **FR-2**: The highwater cursor advanced by FR-1 is the *maximum* item
  key committed so far for that source within the run (sub-batches are
  processed in deterministic read order), and the final post-source
  value equals the legacy source-end value.
- **FR-3**: A new `extraction.concurrency` knob (int, default 1) bounds
  the number of in-flight LLM extraction calls per source batch
  (ThreadPool fan-out around the supersede → extract → materialize block
  per item); `1` preserves today's serial behavior.
- **FR-4**: The LLM transport in `entities.extract` (urllib call,
  120s timeout) retries once on HTTP 5xx (house style: mirror
  `llm.py:74-77` / `embedding.py:156-160` retry-once-on-5xx) before
  raising; timeouts are not retried (deterministic failure, logged).
- **FR-5**: Per-item extraction failure handling is unchanged in
  contract: warning log, remaining items proceed; no new table, no new
  operator surface (failure ledger = feature 020).
- **FR-6**: The new knob(s) are added to `config/knobs.py`,
  `config.example.yml`, and knob docs together (the knob-doc sync guard
  `tests/unit/test_knob_docs.py` stays green); knob defaults are
  safe/legacy-preserving.
- **FR-7**: No host-specific value leaks into the shipped code/config
  defaults/docs (Constitution I — Portability, NFR-13): sub-batch size
  and concurrency are config knobs with portable defaults; the OCI
  override stays in the OCI box's `kb.local.yml`.

## Success Criteria

- A 15,000-item fs run that dies at item 5,000 resumes on the next run
  from item 5,000 (not 0): measured by the source `read()` call count —
  items ≤ the last committed sub-batch's keys are not re-yielded.
- A 15,000-item pass produces ≥15 visible Qdrant count increases (one
  per sub-batch) instead of one final jump.
- With a simulated LLM endpoint, `extraction.concurrency=4` reduces
  extraction wall-clock vs `=1` by a factor approaching 4 (bottleneck-
  bound), with identical materialized-graph results.
- A 5xx-on-first-try LLM response succeeds on the retry (no per-item
  failure logged) under `extraction.concurrency=1`.
- `tests/unit/test_knob_docs.py` + the full existing test suite green.

## Key Entities

- `highwater (source, item_key, last_key)` — now written per sub-batch
  instead of only at source-end.
- `pipeline.sub_batch_size` — new int knob (default 1000).
- `extraction.concurrency` — new int knob (default 1).
- `_run_extraction` worker fan-out (thread pool, bounded by the knob).
- `entities.extract` LLM transport — gains 5xx retry-once.

## Out of Scope (feature 020)

- Per-item failure ledger (`audit_items` table), coverage/status
  scripts (`scripts/kb_coverage.py`, `scripts/kb_status.py`),
  single-ingest lock, stale `partial` audit-row cleanup — these are the
  traceability + contention items (issues 4/5) and land in the
  companion change (020).

## Assumptions

- `fs.read(since)`'s `ts <= since` filter is the correct resume
  primitive (mtime-based); the fs source's read order is deterministic
  (`sorted(rglob)`), so sub-batch order is stable across runs.
- ThreadPool fan-out around extraction is safe: `materialize`/
  `supersede` are idempotent Cypher MERGEs and Neo4j connection
  sessions are per-call (verified in `entities.py`; `:Entity` composite
  `(name,type)` index exists per feature 019-019 graph-index DDL).
- One PR: FR-1..FR-7 together (the three issues share the pipeline
  commit path; splitting them would re-touch the same lines).

## Clarifications (019)

Resolved during the clarify pass (2026-10-22) — these answers are now
binding for planning:

1. **Cursor value type (Q1).** `highwater.last_key` stores the source's
   `IngestItem.key` (string), not `item.ts`. `fs.read(since)` filters
   `ts <= since` where `ts` is an ISO-8601 mtime string — so for `fs`,
   the cursor filter compares an mtime-string against a stored *path*
   string, and **no fs item is ever skipped by it** (that is the root
   cause of the perpetual full re-walk on the OCI box).
   **Decision:** 019 does *not* redesign the cursor type per source.
   019's checkpoint contract is: after each sub-batch, upsert highwater
   for every item in that sub-batch (key → key, the existing
   `upsert_highwater` call, just moved from source-end into the
   sub-batch loop). This makes the *per-item* highwater rows correct and
   populated (fixing the 0-rows symptom) and is prerequisite-neutral:
   NFR-1 dedup (content-addressed point IDs) is untouched, and no source
   contract changes. A ts-aligned cursor (true skip-optimization for
   fs) is a follow-up decision (recorded as open question, not in 019
   scope) because it touches the `highwater` value semantics for *all*
   sources and risks re-ingest gaps for window-based sources.

2. **Sub-batch size default (Q2).** Default `pipeline.sub_batch_size`
   = 1000 items. Rationale: OCI's fs source reads ≤15,000 items
   (`max_items` override) → 15 sub-batches; each Qdrant commit stays
   small enough to keep `wait=True` fast; crash-loss is bounded to one
   sub-batch. Runs reading <1000 items stay single-batch (identical
   behavior to legacy — existing tests unaffected).

3. **Concurrency fan-out model (Q3).** ThreadPool only around the
   extraction block (supersede → extract → materialize), bounded by
   `extraction.concurrency`. The Qdrant upsert and graph-upsert commit
   path stays serial and synchronous (it is fast; the LLM call is the
   bottleneck). No process-level parallelism, no queue.

4. **Retry semantics (Q4).** Retry-once on HTTP 5xx only, at the
   `entities.extract` transport (the raw urllib call), mirroring
   `llm.py`/`embedding.py` house style. Timeouts (`socket.timeout` /
   `URLError`) are NOT retried — they surface as a logged per-item
   failure (existing isolation contract). The retry is invisible to
   `materialize`: it only affects whether `extract` returns or raises.

5. **Dry-run (Q5).** `dry_run=True` skips highwater checkpointing,
   graph writes, and extraction entirely (as today); sub-batching
   affects only the dry-run point count (which is invariant to
   sub-batch size — the count is per-item chunk count, summing to the
   same total).

6. **`sub_batch_size=0`** means "no sub-batching" (legacy single-batch
   path) — the knob's documented escape hatch for debugging/parity
   with pre-019 behavior; the *default* remains 1000.

7. **Knob-doc sync (Q6).** Both new knobs (`pipeline.sub_batch_size`,
   `extraction.concurrency`) are added to `config/knobs.py`,
   `config.example.yml`, and the knob-doc guard
   (`tests/unit/test_knob_docs.py`) in the same commit — the guard
   walks `KNOBS` and the example file, so both must move together.

## Open Questions (deliberately deferred — out of 019 scope)

- **O1 (ts-aligned cursor).** Whether to change `highwater.last_key`
  semantics from "item key" to "item ts" so `fs.read(since)` actually
  skips committed items. Requires a decision across all 8 built-in
  sources (some are window-based and ignore `since` entirely — hermes,
  pi, imap_mail, dsh use fixed look-backs; paperclip uses a SQL
  `> since`). Candidate follow-up feature; 019's per-sub-batch
  checkpoint is still correct-and-needed regardless (it populates the
  per-item rows that today never get written, which is the observable
  "highwater.fs = 0 rows" symptom).
- **O2 (per-run highwater, issue 1-B).** Partition the cursor by
  `run_id` for concurrent multi-user ingest. Only needed if concurrent
  multi-user ingest becomes a requirement; 019 + the 020 single-writer
  lock (5-A) makes concurrent full-tree runs impossible to stack, so
  a global cursor is safe in the meantime.
