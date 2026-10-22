# Issue 6 — Extraction failure strands prior graph edges (`supersede`-before-`extract` ordering)

Status: **FIXED** (019b — `specs/019-ingest-reliability-traceability/spec-issue-6.md`; supersede-after-extract + `kb_extraction_heal` targeted heal) · 2026-10-22 · severity: **medium** (silent graph degradation, not corruption)
Companion to: `docs/ingest-reliability-traceability.md` (issues 1–5).

## One-liner

In `_run_extraction`, `supersede(item)` runs **before** `extract(item)`. `supersede` soft-deletes the item's **live** `MENTIONED`/`REL` edges (stamps `valid_to`). If `extract` then **fails** (LLM timeout / non-JSON — the two failures already in the live journal), the `except` swallows the error and `materialize` never runs, so the item's graph edges are left **soft-deleted with no replacement**. Re-ingest is *lossy*, not idempotent, for the graph layer.

## Root cause (code trace)

Per-item loop in `digital_twins/ingest/pipeline.py` `_run_extraction`:

| line | call | effect |
|---|---|---|
| `469` | `entities.supersede(item.key, …)` | **before** extraction — soft-deletes this item's live `MENTIONED` + `REL` edges (`entities.py:334-354`: `SET m.valid_to = now`) |
| `470` | `extraction = entities.extract(…)` | LLM call — **may throw** (`ExtractionError` / `TimeoutError` / non-JSON) |
| `477` | `entities.materialize(…)` | re-issues edges (`ON MATCH SET m.valid_to = NULL`) — **only reached on success** |
| `487-490` | `except Exception: logging.warning(…)` | logs and **moves to next item** — `materialize` never runs on failure |

Net state when `extract` fails:
- `supersede` already set `valid_to` on all live edges for that `item_id` → they are now "dead".
- `materialize` (which would `ON MATCH` re-set `valid_to = NULL`) was skipped.
- Result: the item's `MENTIONED`/`REL` edges are **dangling/soft-deleted** until a *future successful* extraction re-materializes them.

Why it bites in this deployment specifically:
- Qdrant point IDs are content-addressed + dedup'd (`ids.point_id_s4`, `ids.py:29`), so the **vector store is idempotent** — re-ingest is a safe upsert.
- But because `highwater.source='fs'` is **0** (`docs/ingest-reliability-traceability.md` Issue 1), **every sweep re-reads and re-extracts the entire tree**. So every already-ingested item goes through this `supersede→extract` path again — and any flaky LLM call strands its edges.
- `supersede` is a **soft** delete (`valid_to`), not hard `DETACH DELETE` — so nothing is destroyed; the edges become invisible to `WHERE valid_to IS NULL` queries until re-materialized.

## Impact

- **Silent graph degradation:** failed extractions don't error the run (per-item isolation is intended), so the store "succeeds" while the knowledge graph quietly loses edges for those items.
- **Non-idempotent re-ingest (graph):** violates the spirit of NFR-1 parity — the graph does not converge to the same state on a no-change re-run if any LLM call is flaky.
- **Traceability blind spot:** there's no record of *which* items lost edges (no per-item failure ledger — Issue 4), so the loss is not queryable until it's detected by graph diff.
- **Converges on next success** — but a permanently-flaky item (e.g. one that always times out) stays edge-less indefinitely.

## This is a *reliability + traceability* issue (not just perf)

It's the one case where "is the ingestion idempotent?" answers **no** for the graph layer: Qdrant = yes, Neo4j = yes on success / **no on extraction failure**. The fix is ordering + a guard, and it pairs with Issue 4 (failure ledger) and Issue 2 (retry).

## Fix options (ranked)

**6-A (recommended) — move `supersede` to *after* a successful `extract`.** `supersede`'s only role is to invalidate the *previous* extraction's edges before the new ones are materialized. It's strictly safer to:
```
extraction = extract(...)      # 1. get the new extraction (may fail → no state mutated)
if failed: log + record in audit_items + continue   # prior edges stay live
supersede(item)               # 2. only now invalidate the prior live edges
materialize(item, extraction) # 3. re-issue live edges
```
- If `extract` fails, **nothing is mutated** — prior edges remain valid. Idempotent-by-default.
- One-line-ish reorder; no new schema.

**6-B (additive, do with 6-A) — rollback on failure.** Keep supersede-first (if that ordering is ever required) but on `except`, *re-run* a targeted `supersede`-inverse: `SET m.valid_to = NULL` on this item's just-soft-deleted edges (restore), or simply defer supersede to inside `materialize` only. Belt-and-suspenders with 6-A.

**6-C (with Issue 4) — record failures, then heal.** Add the `audit_items` per-item failure table; a periodic **heal pass** re-runs extraction only for items with `status=failed` and valid_to-dangling edges, converging the graph without a full re-walk.

**6-D (with Issue 2) — retry before strand.** Retrying extraction (2-B) reduces the *frequency* of the strand but doesn't remove it; combine with 6-A so a *terminal* failure (all retries exhausted) no longer strands edges.

## Recommended sequence

1. **6-A** (reorder `supersede` after successful `extract`) — removes the loss by construction.
2. **+ 6-C** (`audit_items` + heal pass) — makes any residual loss *detectable and repairable*.
3. **+ 2-B** retry — reduces failure frequency.
4. **Test-First (constitution):** add a unit test that stubs `extract` to raise, asserts the item's pre-existing `MENTIONED`/`REL` edges are **still live** (`valid_to IS NULL`) after the failed extraction. This test fails on current code (edges get soft-deleted) and passes once 6-A lands — that's the acceptance gate.

## Out of scope / notes

- Does not change the atomic-commit, highwater, or dedup behavior.
- Soft-delete (`valid_to`) semantics are intentional (graph time-travel) — the fix is *when* to soft-delete, not *whether* edges can be invalidated.
- Should land as a Spec-Kit feature (next available spec number, likely **014**): `specify → clarify → plan → tasks → implement`, SDD ledger in `.superpowers/sdd/`, `specs/<n>/tasks.md` checkboxes kept in sync. Pair with Issue 2 (concurrency + retry) and Issue 4 (`audit_items`) since the heal pass (6-C) depends on the failure table.

## References

- `digital_twins/ingest/pipeline.py:416-490` (`_run_extraction`, the `supersede`→`extract`→`materialize` loop + `except`)
- `digital_twins/ingest/entities.py:334-354` (`supersede`), `365-461` (`materialize`)
- `digital_twins/ingest/ids.py:29-39` (`point_id_s4` — why Qdrant is idempotent)
- Live evidence: dt-mcp journal 2026-10-22 `extraction failed … LLM call failed: timed out` / `LLM returned non-JSON` on `研发/…`, `服务/…` items
- `docs/ingest-reliability-traceability.md` — Issues 1, 2, 4
