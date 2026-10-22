# Tasks: 019 — Ingest reliability (resumable sub-batched pipeline, concurrent retried extraction)

(Mirrors `openspec/changes/019-ingest-reliability/tasks.md` if present,
else this file is the SDD ledger.)

## 1. Unit tests (RED-first)
- [x] 1.1 `tests/unit/test_pipeline_subbatch.py` (new): fake source
      yielding 2500 items (3 sub-batches at default 1000), fake
      qdrant/neo4j records call order; assert:
        - 3 embed calls, 3 qdrant upserts, 3 graph upserts, 3
          highwater-checkpoint batches (highwater rows exist after
          sub-batch 1, not only at source end)
        - sub-batch 2 does not re-embed sub-batch 1's items
        - `sub_batch_size=0` → legacy single-batch behavior (one
          highwater batch at source end)
        - dry-run: no highwater rows, no upserts, point count invariant
      **Verification:** tests fail before step 2.
- [x] 1.2 `tests/unit/test_pipeline_extraction.py` (extend):
        - fake LLM transport; `extraction.concurrency=4` materializes
          the same entity set as `=1` (deterministic MERGEs)
        - 5xx-then-200: item succeeds, no failure logged
        - persistent 5xx: item logged as failed, remaining items
          proceed (isolation)
        - timeout: NOT retried (single call), logged
      **Verification:** tests fail before steps 2.2/2.3.

## 2. Implementation
- [x] 2.1 `digital_twins/config/knobs.py` + `digital_twins/config.example.yml`
      together: add `pipeline.sub_batch_size` (int, default 1000, env
      `KB_PIPELINE__SUB_BATCH_SIZE`, 0 = legacy) and
      `extraction.concurrency` (int, default 1, env
      `KB_EXTRACTION__CONCURRENCY`).
      **Verification:** `tests/unit/test_knob_docs.py` green.
- [x] 2.2 `digital_twins/ingest/entities.py`:
        - extract the per-item `supersede → extract → materialize`
          block into a module-level `process_item(...)` helper
        - add 5xx retry-once to the raw urllib LLM transport call
          (mirror `llm.py:74-77` / `embedding.py:156-160`); timeouts
          not retried
      **Verification:** 1.2 retry/isolation tests green.
- [x] 2.3 `digital_twins/ingest/pipeline.py`:
        - read `pipeline.sub_batch_size` (0 → whole list is one
          sub-batch = legacy path)
        - loop: per sub-batch embed chunks → qdrant upsert (wait) →
          graph upsert (if neo4j) → extraction fan-out
          (`ThreadPoolExecutor(max_workers=get(cfg,
          "extraction.concurrency") or 1)` around `process_item`;
          `concurrency=1` → serial) → `upsert_highwater` for every
          item in the sub-batch (main thread, after pool join)
        - keep `total_points` / `counts` / audit-row end-of-source
          semantics; dry-run skips checkpoint+graph+extraction
      **Verification:** 1.1 + 1.2 green; existing pipeline tests
      green unchanged.

## 3. Guards
- [x] 3.1 `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → green.
- [x] 3.2 Full unit + integration suite green (no behavior change at
      defaults: single-sub-batch runs identical to legacy).
- [x] 3.3 Top acceptance check (NFR-1/NFR-14): same content ingested
      via schedule, `run --once`, MCP, web UI → one point.

## 4. Semver + release note
- [x] 4.1 Bump `__version__` (MINOR) + CHANGELOG:
      "pipeline sub-batches commits (resumable highwater checkpoints);
      extraction.concurrency knob + 5xx retry-once on the LLM
      transport."

## 5. Bookkeeping
- [x] 5.1 SDD ledger `.superpowers/sdd/019-ingest-reliability/progress.md`
      (new feature dir, ledger-first).
- [x] 5.2 Spec checkboxes (this file) kept in sync with commits.
