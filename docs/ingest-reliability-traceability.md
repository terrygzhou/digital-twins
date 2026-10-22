# Ingestion reliability & traceability — issues, root causes, solution options

Status: **DRAFT for review** · 2026-10-22 · source of truth: live OCI ingest run
`terryzhou77` (dt-mcp.service, `/data/keyva` fs source) + this package.

Companion to `docs/release-runbook.md` (deploy), `docs/scheduling.md`, and the
SDD ledgers under `.superpowers/sdd/`. This doc is **diagnostic + options**, not
a spec; each fix below should flow through the Spec-Kit path
(`specify → clarify → plan → tasks → implement`) before coding, and the knob
changes must keep `tests/unit/test_knob_docs.py` (the knob-doc sync guard) green.

---

## 0. Observed (OCI, `dt-mcp.service`, live)

- `Qdrant personal_kb`: **14,405** points, `indexed_vectors_count: 0` (HNSW not
  built → searches are full-scan).
- `Neo4j`: `:Entity` ≈ 5,295 · `:MENTIONED` ≈ 12,016 · total nodes ≈ 8,500.
- `state.db → highwater` for `source='fs'`: **0 rows** (`MAX(last_key)=NULL`).
- `state.db → audit_runs`: three `failed` cycles (each `{"fs": 200}`) + two
  in-flight `partial` (`started_at` 20:12 / 20:45 UTC, `completed_at=NULL`).
- Service: ~1.9 core sustained, ~1.3 GB, 15 threads (13 idle `futex_do_wait`,
  2 `R`); **no live socket** to LLM `:4001`, Qdrant `:6333`, or Neo4j `:7687`
  during the freeze — the work is in-memory (in-process BGE-small embed +
  serial LLM extraction).
- OCI `kb.local.yml`: `sources.fs = {enabled:true, max_items:15000,
  extra:{dir:'/data/keyva', exclude:['市场推广']}}`, `extraction.enabled=true`.

Coverage of `/data/keyva` (matched every on-disk file against actual Qdrant
keys): **only ~3,200 of ~14,038 target files (~23%)** represented. Headline
gap: `认证文件` (2,000 files, **0** in the store). `研发`/`text`/`服务` partial.

---

## Issue 1 — No incremental resume: `highwater.fs` is 0 rows, so every run re-reads the whole tree

**Symptom:** Qdrant holds 14,405 points yet `highwater.source='fs'` has zero
rows; the two in-flight runs each re-walk and re-embed the entire ~14k-file
tree from scratch, and neither is finishing (so the cursor never advances, and
the next run redoes 100% of the work again).

**Root cause (code):**
- `upsert_highwater` (`state/models.py:159`) is called **only at source-end**,
  after the whole batch is embedded + graph + extracted — `pipeline.py:326-327`
  (`for item in items: upsert_highwater(...)` runs *after* the `client.upsert`
  at `pipeline.py:306`).
- Because no fs pass has ever completed a 15000-item batch, highwater stayed
  empty. `_cursor(db,"fs")` (`pipeline.py:87-91`, read at `333`) returns `None`
  → `source.read(since=None)` → `fs.read` (`sources/fs.py:28-42`) with
  `since=None` skips the `if since and ts <= since: continue` filter →
  **rglobs all files every time.**
- `sources.fs.max_items=15000` (OCI override, above the 200 default in
  `config/knobs.py:226-231`) makes each pass a full-corpus walk, so the
  never-advancing cursor + full-corpus batch compound into perpetual
  full re-reads. Two concurrent such runs = 2× full-tree work on one LLM
  endpoint + one in-process embedder, both unable to complete.

**Solution options (ranked):**
- **A — checkpoint highwater mid-batch (small, recommended).** Move
  `upsert_highwater` from source-end into the per-sub-batch loop so a
  completed sub-batch advances the cursor; a crash/resume loses ≤ one
  sub-batch and the next run's `since` cursor skips done items. Preserves NFR-1
  (point IDs are content-addressed via `ids.point_id_s4`; highwater is a
  skip-optimization, not the dedup authority).
- **B — per-run highwater (larger).** Partition the cursor by `run_id` so
  concurrent runs don't clobber one global cursor. Needs a schema change
  (`state/models.py` DDL) + `_cursor` change. Only needed if concurrent
  multi-user ingest is a real requirement.
- **C — single-writer discipline (ops, not code).** Serialize ingest so the
  sweep can't stack full-tree runs (see Issue 5). Removes the 2× contention
  that keeps every run from ever completing.

Recommend **A + C**: A gives resumability, C removes the contention that
prevents completion. B only if concurrent ingest is required.

---

## Issue 2 — Serial LLM extraction, no concurrency knob, no retry: day-scale ETA + silent per-item failures

**Symptom:** ~14k items × (in-process BGE embed + one LLM extraction call each,
`timeout=120s`) across *two* concurrent runs sharing one LiteLLM qwen endpoint
→ order of a day, open-ended. Journal shows repeated `LLM call failed: timed
out` and `LLM returned non-JSON` warnings, but no retry and no per-item record.

**Root cause (code):**
- `_run_extraction` (`pipeline.py:416-490`) loops `for item in items:` and
  calls `entities.extract(...)` **one LLM call per item, sequentially** —
  `entities.py:218-303`, transport at `entities.py:204-208`
  (`urllib ... timeout=120`), **no 5xx retry** (unlike the embedding client
  `embedding.py:156-160` and `llm.py:74-77`, which retry-once on 5xx).
- `config/knobs.py` has **no `extraction.concurrency`** knob — only
  `extraction.{enabled, max_text_chars, prompt_version, domain_schema}`
  (`knobs.py:191-214`). So the only lever is disabling extraction.
- Extraction failures are **log-only** (`pipeline.py:487-490`,
  `entities.py:207/254` → `ExtractionError`); the item still gets a Qdrant
  point (extraction is post-upsert), so the file is *searchable* but has **no
  graph entities** — and there is no table recording which items failed.

**Solution options (ranked):**
- **A — add `extraction.concurrency` + fan-out (biggest ETA win, recommended).**
  New int knob (default 1, safe) + a `ThreadPoolExecutor` around the
  `supersede → extract → materialize` block. `materialize`/`supersede` are
  idempotent MERGEs (`entities.py:334-461`) and Neo4j `:Entity` has a
  composite `(name,type)` index (`pipeline.py:388-390`), so concurrent
  materialization is safe. Must be added to `knobs.py` + `config.example.yml`
  together (knob-doc sync guard).
- **B — extraction retry + per-item failure table (recommended, pairs with A).**
  Wrap `extract` in retry/backoff (house style, mirror `llm.py:74-77`), and
  write failures to a new `audit_items` table (Issue 4) so failed items are
  queryable + targeted re-ingestable.
- **C — make extraction a sidecar/queue (larger).** Commit Qdrant+graph
  immediately (already happens per source batch at `pipeline.py:306`/`313`) and
  run LLM extraction as a background step so it can't block the commit. Only if
  the LLM endpoint is the hard ceiling.

Recommend **A + B** in one change; C only if the endpoint is the ceiling.

---

## Issue 3 — Atomic end-of-batch commit: stores look frozen for hours with no progress signal

**Symptom:** For the full duration of a 15000-item pass, Qdrant count, graph
counts, and highwater all sit still; the operator can only infer progress from
CPU TIME. Qdrant HNSW `indexed_vectors_count:0` means even the *completed*
points serve searches by full-scan.

**Root cause (code):**
- All Qdrant upsert (`pipeline.py:306`, `wait=True`), graph upsert
  (`pipeline.py:313`), and extraction (`pipeline.py:317`) happen after the
  *entire* source batch is embedded; highwater at source-end (Issue 1). No
  intermediate commit → no intermediate visibility.
- HNSW index is never built/refreshed in the pipeline (`_ensure_collection`
  at `pipeline.py:94-108` only creates the collection at the pinned dim; it
  does not build/refresh the vector index).

**Solution options (ranked):**
- **A — sub-batch the pipeline (overlaps Issue 1-A, same change).** Split the
  source batch into N (e.g. 500–2000), upsert + commit highwater per sub-batch.
  Frequent visible progress + bounded crash loss.
- **B — build/refresh HNSW after commit (one-liner ops step, recommended).**
  Post-deploy + post-commit, call Qdrant index build so search is actually
  usable; optionally add a pipeline hook to refresh after a large commit.
- **C — progress telemetry (recommended).** Emit per-item
  `count/total/last_item/eta` to the status port (`scheduler.status_port`
  default 8765, `knobs.py:149-153`) so a long run is observable without py-spy.

Recommend **A + C**; **B** as a one-time ops step on the OCI box post-deploy.

---

## Issue 4 — Traceability gap: failures are log-only, no per-item record

**Symptom:** You can't answer "which `认证文件` files failed extraction in run
X?" or "re-ingest only the failed ones." `audit_runs`
(`state/models.py:31-39`) is one row per *run* (status + `per_source_counts`);
extraction failures are `logging.warning` only (`pipeline.py:487-490`).

**Correction to a common assumption:** run-id provenance **already exists** —
`run_id` is stamped in the Qdrant payload (`pipeline.py:299-300`) and on the
Neo4j `SourceItem.last_run_id` / `MENTIONED.run_id` / `REL.run_id`
(`entities.py:407-412`, `424-431`, `447-449`). What is *missing* is a
**failure ledger** and a per-item status query surface.

**Solution options (ranked):**
- **A — add `audit_items` table + write on failure (recommended).** New table
  `(run_id, source, item_key, status, error, ts)`; insert on extraction
  failure (and optionally on success). Queryable + supports targeted
  re-ingest of just the failed items.
- **B — coverage + one-line status tooling (recommended, low-risk).** Promote
  the ad-hoc diagnostics to `scripts/`: `scripts/kb_coverage.py` (per-folder
  on-disk vs in-Qdrant diff) and `scripts/kb_status.py` (one line:
  qdrant_count / graph_entity / last_audit_status / llm_error_rate /
  highwater). Run after every commit to confirm e.g. "`认证文件` 0 → 2000".
- **C — 3-strike alerting.** Consecutive-`failed` detection in
  `scheduler/loop.py` → surface (log/notify).

Recommend **A + B**.

---

## Issue 5 (ops) — Concurrent full-tree runs contended on one LLM + one in-process embedder

**Symptom:** `keyva_sweep.sh` (an **OCI host script, not in this repo**) fired
`kb_ingest` cycles; because the service is a `ThreadingHTTPServer`
(`docs/015-http-mcp-sqlite-threading.md`) that acks 200 immediately and
processes async, each fire spawned a full-tree run that ran **in parallel**
with the prior one — 2× contention, slower, OOM risk. No single-writer lock in
the pipeline or scheduler.

**Solution options (ranked):**
- **A — single-ingest lock (in-repo, recommended, defensive).** A DB advisory
  lock (or `flock`) in `run_pipeline` so overlapping full-tree runs are
  serialized (or rejected with a clear "run already in progress" result).
- **B — sweep discipline (ops).** Cap the sweep to one outstanding run + a
  DONE gate (stop when Qdrant/highwater converge); add `extraction.concurrency`
  (Issue 2-A) so a single run is fast enough that concurrent runs aren't
  needed.

Recommend **A** in-repo + **B** in-ops.

---

## Suggested sequencing

1. **Config-only, no code (do on the OCI box immediately):**
   - `extraction.concurrency` **not yet available** — skip until the knob lands.
   - Pre-build the Qdrant HNSW index (Issue 3-B) so search is usable now.
   - Stop the sweep from stacking runs (Issue 5-B) to remove the 2× contention.
2. **Code change 1 (reliability, unblock the ETA): Issue 1-A + Issue 2-A/B
   + Issue 3-A** — checkpoint highwater, add `extraction.concurrency` +
   fan-out + retry, sub-batch the pipeline. One PR; keep the knob-doc sync
   guard green.
3. **Code change 2 (traceability): Issue 4-A/B + Issue 5-A** — `audit_items`
   table + `scripts/kb_coverage.py` + `scripts/kb_status.py` + single-ingest
   lock.
4. **Validate:** one clean concurrent ingest run, time-boxed, watched to
   commit; confirm `认证文件` goes 0 → 2000 in Qdrant and highwater advances.

---

## Redeploy to OCI (minimal)

Canonical path is `docs/release-runbook.md` (package built as
`digital-twins-kb` on PyPI). OCI specifics for this box:

- Remote: `github.com/terrygzhou/digital-twins` (`main`). OCI target is the
  `dt-mcp.service` venv at `/home/ubuntu/digital-twins-venv`, state at
  `/home/ubuntu/.digital-twins/state.db`, config at
  `/home/ubuntu/.config/digital-twins/kb.local.yml` (the **active** config —
  the repo copy is a template/trap; edit the live one).
- Deploy: land the code (git pull / pip install of the new build) into the
  venv, update `kb.local.yml`, then restart `dt-mcp.service`. On this box the
  reliable restart is `ssh` → `pkill -f digital-twins` then
  `fuser -k 8771/tcp` (the ThreadingHTTPServer otherwise survives a bare
  stop and keeps the old code live).
- No `sqlite3` CLI on the box — inspect `state.db` via
  `python3 -c "import sqlite3; ..."`.
- Verify post-deploy: `Qdrant count` jumps, `highwater.fs` rows > 0, one
  `audit_runs` row closes `ok` (not `partial`/`failed`), HNSW
  `indexed_vectors_count > 0`, and `scripts/kb_coverage.py` shows `认证文件`
  represented.

---

## Reproduce (one-liners, OCI)

```bash
# coverage: on-disk vs in-Qdrant, per top-level folder
python3 scripts/kb_coverage.py            # after issue 4-B lands
# one-line status
python3 scripts/kb_status.py
# raw, until those land:
python3 -c "import qdrant_client as q,c=json" ...  # count points, scan item_key
python3 -c "import sqlite3, glob, json" ...        # audit_runs + highwater.fs
journalctl -u dt-mcp.service -n 50 --no-pager      # extraction failures
```
