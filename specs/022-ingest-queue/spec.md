# Feature 022: Ingest queue (async, observable, retriable ingest runs)

> **License note** (NFR-13 / constitution I): this spec is an
> inspiration-level design reference only. No code, prompts, or test
> fixtures are ported from `nashsu/llm_wiki` (GPL-3.0) into this MIT
> package.

## User Scenarios & Testing

019 sub-batched the pipeline so a crash loses at most one sub-batch.
022 adds an *operator surface* on top: a queue of *document-level*
ingest jobs (not full pipeline runs) with status, retry, and cancel.
A user submits an ingest via MCP (`kb_ingest` / `kb_ingest_folder`)
or the Web; the run is recorded as a queue row, processed
asynchronously by a worker, and the user polls
`kb_ingest_status` / `GET /api/v1/queue` to see per-item progress
and retry a failed item.

**Primary scenario**

1. `queue.enabled: true` (default `false` — off, behaviour
   byte-for-byte unchanged; today's `kb_ingest` is still a
   synchronous `run_pipeline` hand-off).
2. A user calls `kb_ingest_folder` with
   `{"path": "/reports"}` (016's tool) — the tool now
   *enqueues* the run instead of running it inline: it returns
   `{"ok": true, "run_id": "abc123"}` immediately.
3. A background worker picks up `run_id=abc123`, runs the
   pipeline (019's sub-batched `run_pipeline`), and updates the
   queue row as it goes: status `pending → running → done` (or
   `error` on failure), with `progress_pcts` (a JSON array of
   `[item_index, pct]` snapshots) updated per sub-batch.
4. The user calls `kb_ingest_status` with
   `{"run_id": "abc123"}` and gets `{"ok": true, "status":
   "running", "progress_pcts": [[0, 25], [1, 50], [2, 75]],
   "items": [{"key": ..., "status": "written"}, ...]}`.
5. On a failed item, the user calls `kb_ingest_retry` with
   `{"run_id": "abc123", "item_index": 1}` — the worker re-runs
   just that item (the queue row's `attempts` increments; the
   item is re-enqueued, others untouched).

**Edge cases**

- `queue.enabled: false` (default) → `kb_ingest_folder` /
  `kb_ingest` run *inline* exactly as today (no queue row
  written, no async behaviour). The three new MCP tools
  (`kb_ingest_status`, `kb_ingest_retry`, `kb_ingest_cancel`)
  return `{"ok": false, "error": {"code": "queue_disabled"}}`
  (a clean, no-op surface — no crash).
- `queue.max_attempts: 3` (default) exceeded → the queue row
  is marked `error` with `last_error` set; the item is *not*
  auto-retried (an operator must call `kb_ingest_retry`
  explicitly — a v1 safety net against retry storms).
- A run is cancelled (`kb_ingest_cancel` with
  `{"run_id": "abc123"}`) mid-way → the worker finishes the
  current sub-batch (no mid-sub-batch abort — 019's commit
  order holds: Qdrant upsert → graph upsert → highwater
  checkpoint, all or nothing), then the queue row is marked
  `cancelled`; subsequent sub-batches are skipped.
- `queue.stale_after_min: 30` (default) — a queue row in
  `running` with no `progress_pcts` update for >30 minutes is
  marked `error` (stale) on the next worker tick; a new run
  can be retried.
- NFR-13: no host paths in `last_error` or in the MCP
  response — only the caller-supplied `path` is echoed.

## Functional Requirements

- **FR-1**: Add a new SQLite table `ingest_queue` in
  `digital_twins/state/db.py` (+ migration in
  `digital_twins/state/migrations.py`):
  `(id TEXT PK, source_key TEXT, document_key TEXT, status TEXT
  CHECK(status IN ('pending','running','done','error','
  cancelled')), attempts INTEGER, last_error TEXT,
  enqueued_at TEXT, started_at TEXT, finished_at TEXT,
  progress_pcts TEXT)` where `progress_pcts` is a JSON array
  of `[item_index, pct]` snapshots (the 019 sub-batch commit
  hook writes a new snapshot per sub-batch).
- **FR-2**: Add `digital_twins/ingest/queue.py`:
  `enqueue(source_key, document_key, cfg) -> run_id` (INSERT
  a `pending` row; returns the `id`), `dequeue(worker_id,
  cfg) -> run_id | None` (an atomic `UPDATE ... SET status =
  'running' WHERE status = 'pending' ORDER BY enqueued_at
  LIMIT 1 RETURNING id` — SQLite's `RETURNING` (3.35+)),
  `progress(run_id, item_index, pct)` (append to
  `progress_pcts`), `finish(run_id, status, last_error =
  None)` (set `status`, `finished_at`; on `error`, also set
  `last_error`).
- **FR-3**: A background worker
  (`digital_twins/ingest/queue_worker.py`, a `threading.Thread`
  started when `queue.enabled: true` and the MCP/Web server
  boots) loops: `dequeue` → `run_pipeline` (019's sub-batched
  variant) → `progress` per sub-batch → `finish`. The worker
  is a *single* writer (no concurrent `run_pipeline` on the
  same source — 019's 5-A single-writer lock holds).
- **FR-4**: `kb_ingest_folder` (016) and `kb_ingest` (007),
  when `queue.enabled: true`, call `enqueue` instead of
  `run_pipeline` inline: the tool returns
  `{"ok": true, "run_id": "..."}` (a new response field; the
  legacy synchronous path is unchanged when the knob is off).
- **FR-5**: Add three new MCP tools to
  `digital_twins/mcp/registry.py` + `digital_twins/mcp/
  dispatch.py`:
  - `kb_ingest_status` with inputSchema `{run_id: string
    (required)}` — returns `{"ok": true, "status": ...,
    "attempts": n, "last_error": ... | null,
    "progress_pcts": [...], "items": [...]}` (read-only;
    role: `reader`).
  - `kb_ingest_retry` with inputSchema `{run_id: string
    (required), item_index: int (optional)}` — re-enqueues a
    failed item (or the whole run when `item_index` is
    omitted); the queue row's `attempts` increments; a new
    `pending` row is INSERTed (the old row is kept,
    `attempts` reflects the new attempt). Role: `scheduler`
    (the 004 ACL gate — reader cannot retry).
  - `kb_ingest_cancel` with inputSchema `{run_id: string
    (required)}` — sets the queue row to `cancelled`; the
    worker (mid-sub-batch) honours the flag on its next
    sub-batch boundary (no mid-sub-batch abort — 019's
    commit order holds). Role: `scheduler`.
- **FR-6**: Add a new Web endpoint `GET /api/v1/queue` (015's
  HTTP layer): `{"ok": true, "rows": [{"id", "source_key",
  "document_key", "status", "attempts", "last_error",
  "enqueued_at", "started_at", "finished_at",
  "progress_pcts"}]}` (the same rows as the queue table;
  newest first, default limit 100). Role: any authenticated
  user (the same gate as `kb_search`).
- **FR-7**: Add knobs `queue.enabled` (bool, default `false`),
  `queue.stale_after_min` (int, default 30), `queue.max_
  attempts` (int, default 3) to `config/knobs.py` +
  `config.example.yml` + knob docs together (`tests/unit/
  test_knob_docs.py` green).
- **FR-8**: Test-First. Unit tests use a temp SQLite DB and
  assert:
  - `enqueue` → a `pending` row; `dequeue` → a `running` row
    with `started_at` set.
  - `progress(run_id, 0, 25)` then `progress(run_id, 1, 50)`
    → `progress_pcts` JSON is `[[0, 25], [1, 50]]`.
  - `finish(run_id, "error", last_error="boom")` → the row is
    `error` with `last_error` set.
  - `kb_ingest_retry` with `item_index` → a new `pending` row
    with `attempts` = old `attempts` + 1; the old row is
    unchanged.
  - `kb_ingest_cancel` mid-run (the worker is in a sub-batch)
    → the worker finishes the current sub-batch (all-or-
    nothing commit order), then marks the row `cancelled`;
    subsequent sub-batches are skipped.
  - `queue.stale_after_min` exceeded (no `progress_pcts`
    update for >30 min, simulated by a clock) → the next
    worker tick marks the row `error` (stale).
  - `queue.enabled: false` → `kb_ingest_folder` is synchronous
    (no queue row, no `run_id` in the response), the three new
    MCP tools return `queue_disabled`.
  - `tests/unit/test_knob_docs.py` green.

## Non-goals

- No change to 002's scheduler semantics — the queue is an
  *operator* surface, not a new scheduler job (the 002
  scheduler is unchanged; the queue worker is a new thread,
  not a new schedule).
- No cross-source parallelism — the queue worker is a single
  writer (019's 5-A single-writer lock; concurrent full-tree
  runs are still impossible to stack).
- No persistent-queue backpressure — the queue is in-memory-
  bounded (the SQLite table is the source of truth; a very
  large queue is an operator concern, not a v1 safety net).
- No change to 019's sub-batch commit order — the queue's
  `progress_pcts` snapshots are written *after* each sub-
  batch's highwater checkpoint (never before — the snapshot
  is a *post*-commit visibility marker).
- No change to 007's `kb_search` / `kb_chat` (the queue is a
  separate surface; the three new tools do not alter any
  existing tool body).
- No host paths in `last_error` (NFR-13) — the error message
  is the exception's string, never an absolute host path.

## Success Criteria

- With `queue.enabled: true`, a `kb_ingest_folder` call
  returns a `run_id` in <1 s (the enqueue is an INSERT, not
  a pipeline run).
- `kb_ingest_status` polls a running run and returns
  `progress_pcts` that grows monotonically (the 019 sub-
  batch commit hook appends a new snapshot per sub-batch).
- `kb_ingest_retry` on a failed item re-runs just that item
  (the queue row's `attempts` increments; other items are
  untouched).
- `kb_ingest_cancel` mid-sub-batch → the worker finishes the
  current sub-batch (all-or-nothing commit order holds),
  then marks the row `cancelled`.
- `queue.stale_after_min` (default 30) exceeded → the next
  worker tick marks the row `error` (stale).
- `queue.enabled: false` (default) → the three new MCP tools
  return `queue_disabled`, `kb_ingest_folder` is synchronous
  (byte-for-byte unchanged), no queue table row is written.
- `tests/unit/test_knob_docs.py` green.

## Key Entities

- `ingest_queue (id, source_key, document_key, status,
  attempts, last_error, enqueued_at, started_at, finished_at,
  progress_pcts)` — new SQLite table.
- `enqueue` / `dequeue` / `progress` / `finish` — new
  functions in `digital_twins/ingest/queue.py`.
- `digital_twins/ingest/queue_worker.py` — a new background
  thread (single writer).
- `kb_ingest_status`, `kb_ingest_retry`, `kb_ingest_cancel` —
  new MCP tools.
- `GET /api/v1/queue` — new Web endpoint.
- `queue.enabled`, `queue.stale_after_min`,
  `queue.max_attempts` — new config knobs.

## Assumptions

- SQLite's `RETURNING` clause (3.35+) is available in the
  operator's environment (the package already depends on
  SQLite; the `RETURNING` clause is a standard 3.35+ feature).
- The queue worker is a single writer (the 019 5-A single-
  writer lock is in effect — no concurrent `run_pipeline` on
  the same source).
- The `progress_pcts` JSON array is append-only (a new
  snapshot per sub-batch; never a rewrite of an existing
  snapshot — the array grows monotonically).
- The three new MCP tools are *additive* (they do not alter
  any existing tool body; the legacy synchronous path is
  unchanged when `queue.enabled: false`).

## Open questions (max 3)

- [NEEDS CLARIFICATION: should `kb_ingest_retry` accept a
  `source_key` filter (retry only items from a specific
  source) or is `run_id` + `item_index` sufficient for v1?] —
  default: `run_id` + `item_index` (a source filter is a
  trivial v1.1 add).
- [NEEDS CLARIFICATION: should `GET /api/v1/queue` accept a
  `status` query parameter (filter by `pending` / `running` /
  `done` / `error` / `cancelled`)?] — default: yes, a
  `status` query parameter (a standard filter, not a new
  table).

## Semver

MINOR. New optional feature behind a default-off knob
(`queue.enabled`); when off, the synchronous `kb_ingest` /
`kb_ingest_folder` path is byte-for-byte unchanged and the
three new MCP tools return a clean `queue_disabled` (no
crash). New optional knobs + a new SQLite table are additive.

