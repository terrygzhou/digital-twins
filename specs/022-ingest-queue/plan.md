# Plan: 022 — Ingest queue (default-off background ingest with progress, retry, cancel)

## Summary

Adds a default-off ingest queue: when `queue.enabled: true`,
`kb_ingest_folder` (016) and `kb_ingest` (007) enqueue a job instead of
running the pipeline inline, and a single background worker thread runs
019's sub-batched `run_pipeline` for each queued job, writing
`progress_pcts` snapshots after each sub-batch commit. Three new MCP
tools (`kb_ingest_status`, `kb_ingest_retry`, `kb_ingest_cancel`) and a
new Web endpoint (`GET /api/v1/queue`) give operators the
status / retry / cancel surface. Stuck jobs (`queue.stale_after_min`
exceeded without a progress update) are marked `error` on the next
worker tick. With the knob off (the default), everything is
byte-for-byte unchanged: no queue row is written, the two ingest tools
run synchronously as before, and the three new tools return
`queue_disabled`.

## Approach

1. **Knobs (RED-first).** Add `queue.enabled` (bool, default `false`),
   `queue.stale_after_min` (int, default `30`), `queue.max_attempts`
   (int, default `3`) to `digital_twins/config/knobs.py` (a new
   `GROUP_QUEUE` group constant), `config.example.yml`,
   `.env.example` (`KB_QUEUE__ENABLED`, `KB_QUEUE__STALE_AFTER_MIN`,
   `KB_QUEUE__MAX_ATTEMPTS`), and `docs/configuration.md` — so the
   `tests/unit/test_knob_docs.py` guard (T027) stays green.
2. **Schema (migration v5).** `apply_v5` in
   `digital_twins/state/models.py` creating the `ingest_queue` table
   (`id TEXT PRIMARY KEY`, `source_key`, `document_key`,
   `status CHECK IN ('pending','running','done','error','cancelled')`,
   `attempts INTEGER NOT NULL DEFAULT 0`, `last_error`,
   `enqueued_at`, `started_at`, `finished_at`, `progress_pcts` — a JSON
   array of `[item_index, pct]` snapshots, plus an index on
   `(status, enqueued_at)`); appended to `MIGRATIONS` in
   `digital_twins/state/migrations.py` as `(5, apply_v5)` so
   `SCHEMA_VERSION` auto-derives to 5. Purely additive
   (`CREATE TABLE IF NOT EXISTS`), like v3/v4.
3. **Queue core.** `digital_twins/ingest/queue.py` (new):
   `enqueue(db, source_key, document_key, cfg) -> run_id` (INSERT a
   `pending` row, return the id), `dequeue(db, cfg) -> row | None`
   (atomic `UPDATE ... SET status='running', started_at=? WHERE
   status='pending' ORDER BY enqueued_at LIMIT 1 RETURNING *` —
   SQLite 3.35+), `progress(db, run_id, item_index, pct)` (append
   `[item_index, pct]` to the `progress_pcts` JSON array),
   `finish(db, run_id, status, last_error=None)` (set `status` +
   `finished_at`; on `error` also `last_error`), plus
   `mark_stale(db, cfg)` (running rows whose last `progress_pcts`
   snapshot is older than `queue.stale_after_min` → `error`,
   `last_error="stale: no progress update for {n} min"`). `run_id` is
   a fresh `uuid4()` hex string.
4. **Worker.** `digital_twins/ingest/queue_worker.py` (new): a
   `threading.Thread` subclass (`IngestQueueWorker`) —
   `start()`/`stop()` (daemon thread, `threading.Event` stop flag),
   a `tick()` loop that runs `mark_stale` → `dequeue` → runs the
   queued item through the pipeline → `progress` per sub-batch →
   `finish`. The worker is the *single* writer: it holds the 019
   5-A ingest lock for the source it is running, so no concurrent
   `run_pipeline` on the same source can stack. Cancel is checked at
   sub-batch boundaries only: if the row was set to `cancelled`
   between sub-batches, the worker finishes the current sub-batch
   (019's commit order — Qdrant upsert → graph upsert → highwater
   checkpoint — all-or-nothing), then marks the row `cancelled` and
   skips the remaining sub-batches. A progress hook is passed into
   `run_pipeline` (019's sub-batched loop already runs per
   sub-batch; the hook appends one `progress_pcts` snapshot per
   sub-batch *after* its highwater checkpoint).
5. **Enqueue path.** `digital_twins/mcp/dispatch.py`: when
   `queue.enabled` is true, `kb_ingest_folder` / `kb_ingest` call
   `enqueue` and return `{"ok": true, "run_id": "..."}` — a new
   response field; the legacy synchronous path is unchanged when the
   knob is off. `document_key` for `kb_ingest_folder` is the folder
   path (a caller-supplied argument, never a host path — NFR-13);
   for `kb_ingest` it is the source name.
6. **MCP tools.** `digital_twins/mcp/registry.py` +
   `digital_twins/mcp/dispatch.py`: three additive tools —
   `kb_ingest_status` `{run_id: str (required)}` (read-only, role
   `reader`) returning `{"ok": true, "status", "attempts",
   "last_error", "progress_pcts", "items"}`; `kb_ingest_retry`
   `{run_id: str, item_index: int (optional)}` (role `scheduler` via
   the 004 ACL gate) — INSERTs a new `pending` row for the failed
   item (or the whole run when `item_index` is omitted) with
   `attempts` = old + 1; `kb_ingest_cancel` `{run_id: str}` (role
   `scheduler`) — sets the row to `cancelled`. All three return
   `{"ok": false, "error": {"code": "queue_disabled"}}` when the knob
   is off. No existing tool body is altered.
7. **Web endpoint.** `digital_twins/web/app.py`:
   `GET /api/v1/queue` (bearer-gated, same gate as `kb_search`),
   optional `?status=` query param, returning `{"ok": true, "rows":
   [{id, source_key, document_key, status, attempts, last_error,
   enqueued_at, started_at, finished_at, progress_pcts}]}` — newest
   first, default limit 100.
8. **Worker boot.** The worker thread is started when
   `queue.enabled: true` and the MCP HTTP server
   (`digital_twins/mcp/http.py`) or the Web server
   (`digital_twins/web/app.py` `WebBind` start path) boots — a
   single daemon thread per process, stopped with the server. The
   002 scheduler is *not* touched (non-goal).
9. **Guard tests.** `tests/integration/test_portability.py` (T006)
   and `tests/unit/test_knob_docs.py` (T027) stay green; the new
   queue code never references a host path (`last_error` carries the
   exception string, never an absolute host path — NFR-13).

## Files touched

- `digital_twins/config/knobs.py` — +3 knobs, +`GROUP_QUEUE`
- `config.example.yml` — +`queue:` section
- `.env.example` — +3 `KB_QUEUE__*` lines
- `docs/configuration.md` — +Queue section
- `digital_twins/state/models.py` — +`apply_v5`
- `digital_twins/state/migrations.py` — +`(5, apply_v5)`
- `digital_twins/ingest/queue.py` — new
- `digital_twins/ingest/queue_worker.py` — new
- `digital_twins/ingest/pipeline.py` — progress-hook parameter
- `digital_twins/mcp/registry.py` — +3 tool schemas
- `digital_twins/mcp/dispatch.py` — enqueue path + 3 tool handlers
- `digital_twins/mcp/http.py` — worker thread boot/shutdown
- `digital_twins/web/app.py` — `GET /api/v1/queue`
- `tests/unit/test_queue.py` — new (FR-8 suite)
- `tests/unit/test_knob_docs.py` — green (no edit expected; docs keep
  it in lock-step)
- `digital_twins/__init__.py` — version bump to `0.16.0`

## Open questions

- ~~Should `kb_ingest_retry` accept a `source_key` filter?~~ Resolved
  in the spec: `run_id` + `item_index` only; a `source_key` filter is
  a trivial v1.1 addition.
- ~~Should `GET /api/v1/queue` accept a `status` query param?~~
  Resolved in the spec: yes (default: all rows).

No unresolved items remain.

## Semver

MINOR — `0.15.0` → `0.16.0`. All changes are additive and
default-off: three new default-off knobs, a new SQLite table
(`ingest_queue`), a new background thread (started only when
`queue.enabled: true`), three new MCP tools, one new Web endpoint. No
existing tool body, endpoint, or default is altered — with the knobs
off, behavior is byte-for-byte unchanged (constitution VI, NFR-15).
New minor feature, no breaking change.
