# Solution: 022 — Ingest queue (default-off background ingest)

## What was built

A default-off background ingest queue. When `queue.enabled: true`,
`kb_ingest_folder` (016) and `kb_ingest` (007) **enqueue** a job
instead of running the pipeline inline: a single daemon worker thread
per process picks up the next `pending` row and runs 019's
sub-batched `run_pipeline` end-to-end, writing a `progress_pcts`
snapshot after each sub-batch commit. Three new MCP tools and one new
Web endpoint give operators the status / retry / cancel surface. With
the knob off (the default) everything is byte-for-byte unchanged: no
queue row is written, the two ingest tools run synchronously as
before, and the three new tools return `queue_disabled`.

## Design decisions

### 1. Knobs — `digital_twins/config/knobs.py`

Three new knobs under a new `GROUP_QUEUE` group constant, all
default-off:

| Knob | Type | Default | Env var |
|------|------|---------|---------|
| `queue.enabled` | bool | `false` | `KB_QUEUE__ENABLED` |
| `queue.stale_after_min` | int | `30` | `KB_QUEUE__STALE_AFTER_MIN` |
| `queue.max_attempts` | int | `3` | `KB_QUEUE__MAX_ATTEMPTS` |

- `queue.enabled: true` → enqueue path (MCP tools return `run_id`
  immediately; worker runs the pipeline in the background).
- `queue.stale_after_min` → running jobs whose last progress
  snapshot is older than this threshold are marked `error` on the
  next worker tick.
- `queue.max_attempts` → a retry beyond this cap → `error`,
  `last_error="max_attempts exceeded"`.
- Documented in `config.example.yml`, `.env.example`, and
  `docs/configuration.md` (T027 guard stays green).

### 2. Schema migration v5 — `digital_twins/state/models.py` +
`migrations.py`

`apply_v5` creates the `ingest_queue` table (additive,
`CREATE TABLE IF NOT EXISTS`, like v3/v4):

```
ingest_queue (
  id           TEXT PRIMARY KEY,       -- uuid4 hex
  source_key   TEXT,
  document_key TEXT,                  -- folder path (016) / source name (007);
                                       -- caller-supplied, never a host path (NFR-13)
  status       TEXT CHECK(status IN ('pending','running','done','error','cancelled')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  enqueued_at  INTEGER NOT NULL,       -- unix ts
  started_at   INTEGER,
  finished_at  INTEGER,
  progress_pcts TEXT                   -- JSON array of [item_index, pct]
)
CREATE INDEX idx_ingest_queue_status_enqueued ON ingest_queue(status, enqueued_at);
```

Appended to `MIGRATIONS` in `migrations.py` as `(5, apply_v5)` so
`SCHEMA_VERSION` auto-derives to 5.

### 3. Queue core — `digital_twins/ingest/queue.py` (new)

- `enqueue(db, source_key, document_key, cfg) -> run_id`
  — INSERT a `pending` row, return the fresh `uuid4` hex `run_id`.
- `dequeue(db, cfg) -> row | None`
  — atomic `UPDATE ... SET status='running', started_at=? WHERE
    status='pending' ORDER BY enqueued_at LIMIT 1 RETURNING *`
  (SQLite 3.35+). Returns the row just promoted, or `None` when the
  queue is empty.
- `progress(db, run_id, item_index, pct)`
  — appends `[item_index, pct]` to the `progress_pcts` JSON array.
- `finish(db, run_id, status, last_error=None)`
  — sets `status` + `finished_at`; on `error` also writes
  `last_error`.
- `mark_stale(db, cfg)`
  — running rows whose last `progress_pcts` snapshot is older than
  `queue.stale_after_min` → `error`,
  `last_error="stale: no progress update for {n} min"`.

All functions take the DB handle + cfg explicitly (no module-level
globals), so the tests can inject a temp SQLite DB and a simulated
clock.

### 4. Worker — `digital_twins/ingest/queue_worker.py` (new)

`IngestQueueWorker(db, cfg)` — a `threading.Thread` subclass:

- `start()` — launches the daemon thread; `stop()` — sets the
  `threading.Event` stop flag and joins.
- `tick()` — `mark_stale` → `dequeue` → run the queued item through
  `run_pipeline` with a per-sub-batch progress hook → `progress`
  after each sub-batch's highwater commit → `finish`.
- **Cancel semantics**: the row's `status` is checked at
  sub-batch boundaries only. If a concurrent `kb_ingest_cancel`
  sets it to `cancelled` between sub-batches, the in-progress
  sub-batch completes fully (019's commit order — Qdrant upsert →
  graph upsert → highwater checkpoint — is all-or-nothing), then
  the worker marks the row `cancelled` and skips the remaining
  sub-batches. A cancel mid-sub-batch is not attempted — the
  sub-batch runs to completion.
- **Single writer**: the worker holds the 019 5-A ingest lock for
  the source it is running, so no concurrent `run_pipeline` on the
  same source can stack.
- **Host-path safety (NFR-13)**: `last_error` carries the exception
  string, never an absolute host path.

### 5. Enqueue path — `digital_twins/mcp/dispatch.py`

When `queue.enabled` is true, `kb_ingest_folder` / `kb_ingest` call
`enqueue` and return `{"ok": true, "run_id": "..."}` — a new
response field; the legacy synchronous path is unchanged when the
knob is off. `document_key` is the caller-supplied folder path
(016) or source name (007) — never a host-resolved path.

### 6. Pipeline progress hook — `digital_twins/ingest/pipeline.py`

An optional `progress_hook` parameter was added to the sub-batched
loop (019). `None` (default) → byte-for-byte unchanged. The worker
passes a hook that appends one `progress_pcts` snapshot after each
sub-batch's highwater checkpoint.

### 7. MCP tools — `digital_twins/mcp/registry.py` + `dispatch.py`

Three additive tools (004 ACL gate):

| Tool | Role | Input | Output |
|------|------|-------|--------|
| `kb_ingest_status` | `reader` | `{run_id: str}` | `{"ok": true, "status", "attempts", "last_error", "progress_pcts", "items"}` |
| `kb_ingest_retry` | `scheduler` | `{run_id: str, item_index: int?}` | `{"ok": true, "new_run_id": "..."}` (INSERTs a new `pending` row for the failed item / whole run, `attempts` = old + 1; old row kept) |
| `kb_ingest_cancel` | `scheduler` | `{run_id: str}` | `{"ok": true, "cancelled": true}` (sets the row `cancelled`) |

All three return `{"ok": false, "error": {"code": "queue_disabled"}}`
when the knob is off. No existing tool body is altered.

### 8. Web endpoint — `digital_twins/web/app.py`

`GET /api/v1/queue` (bearer-gated, same gate as `kb_search`),
optional `?status=` query param (default: all rows), newest first,
default limit 100:

```json
{
  "ok": true,
  "rows": [
    {
      "id": "...", "source_key": "...", "document_key": "...",
      "status": "done", "attempts": 1, "last_error": null,
      "enqueued_at": 1718000000, "started_at": 1718000010,
      "finished_at": 1718000120,
      "progress_pcts": [[0, 25], [1, 50], [2, 100]]
    }
  ]
}
```

### 9. Worker boot — `digital_twins/mcp/http.py` + `web/app.py`

When `queue.enabled: true` the `IngestQueueWorker` thread is started
(daemon, one per process) and stopped on server shutdown. The 002
scheduler is **not** touched.

## Semver

MINOR — `0.15.0` → `0.16.0`. All changes are additive and
default-off: three new default-off knobs, a new SQLite table
(`ingest_queue`), a new background thread (started only when
`queue.enabled: true`), three new MCP tools, one new Web endpoint.
No existing tool body, endpoint, or default is altered — with the
knobs off, behavior is byte-for-byte unchanged (constitution VI,
NFR-15). New minor feature, no breaking change.

## What was *not* built (out of scope)

- No watch-mode / continuous ingestion.
- No remote paths (S3/HTTP) — the queue enqueues a folder path or
  source name; the worker resolves it through the same
  `Source.read()` path the synchronous path uses.
- No per-file progress streaming over the MCP transport (004
  contract is request/response JSON); progress is via
  `kb_ingest_status` / `GET /api/v1/queue` polling.
- No `source_key` filter on `kb_ingest_retry` — `run_id` +
  `item_index` only; a `source_key` filter is a trivial v1.1
  addition.

## Verification

- `tests/unit/test_queue.py` + `tests/unit/test_queue_worker.py` —
  all green.
- `tests/integration/test_portability.py` (T006) +
  `tests/unit/test_knob_docs.py` (T027) — both green.
- Full suite `python3 -m pytest tests/ -q` — all green.
