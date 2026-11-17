# Tasks: 022 — Ingest queue (default-off background ingest)

## 1. Knobs (RED-first, constitution Principle III)

- [ ] 1.1 Add `queue.enabled` (bool, default `false`),
      `queue.stale_after_min` (int, default `30`),
      `queue.max_attempts` (int, default `3`) to
      `digital_twins/config/knobs.py` — a new `GROUP_QUEUE` group
      constant — plus a nested `queue:` section in
      `config.example.yml`, the three `KB_QUEUE__*` lines in
      `.env.example`, and a new `## Queue` section in
      `docs/configuration.md`. **Verification:**
      `tests/unit/test_knob_docs.py` (T027) green; fresh config →
      all three knobs absent → defaults apply.

- [ ] 1.2 Unit test: `queue.enabled` off (default) → `kb_ingest_folder`
      is byte-for-byte the legacy synchronous path — no queue row
      written, no `run_id` in the response. **Verification:** test
      green against unmodified dispatch code.

## 2. Schema migration v5 (additive)

- [ ] 2.1 Write unit tests (RED): `apply_v5` creates the
      `ingest_queue` table with the exact column set
      (`id`, `source_key`, `document_key`, `status` CHECK constraint,
      `attempts`, `last_error`, `enqueued_at`, `started_at`,
      `finished_at`, `progress_pcts`) + an index on
      `(status, enqueued_at)`; `SCHEMA_VERSION` derives to 5;
      re-applying v5 on a v5 DB is a no-op (`IF NOT EXISTS`).
      Fixtures: temp SQLite DB; no host paths (NFR-13).
      **Verification:** tests fail before `apply_v5` exists.

- [ ] 2.2 Implement `apply_v5` in `digital_twins/state/models.py`
      (`CREATE TABLE IF NOT EXISTS ingest_queue ...`) + register
      `(5, apply_v5)` in `digital_twins/state/migrations.py`
      `MIGRATIONS`. **Verification:** §2.1 tests green.

## 3. Queue core (RED-first)

- [ ] 3.1 Write unit tests in `tests/unit/test_queue.py` (new file):
      - `enqueue` → `pending` row, `attempts` 0, `run_id` returned
        matches the row's `id`
      - `dequeue` → oldest `pending` row becomes `running` with
        `started_at` set; `None` when no `pending` row
      - `progress(run, 0, 25)` then `progress(run, 1, 50)` →
        `progress_pcts` JSON is `[[0,25],[1,50]]`
      - `finish(run, "error", last_error="boom")` → row `error`,
        `last_error` set, `finished_at` set; `finish(run, "done")`
        → `done`, `last_error` null
      - `mark_stale` with a simulated clock past
        `queue.stale_after_min` → running row → `error`,
        `last_error` names the stale minutes
      - **Verification:** tests fail on current code (module
        absent).
- [ ] 3.2 Implement `digital_twins/ingest/queue.py` (new):
      `enqueue(db, source_key, document_key, cfg) -> run_id`
      (`uuid4` hex), `dequeue(db, cfg) -> row | None` (atomic
      `UPDATE ... SET status='running', started_at=? WHERE
      status='pending' ORDER BY enqueued_at LIMIT 1 RETURNING *`),
      `progress(db, run_id, item_index, pct)`, `finish(db, run_id,
      status, last_error=None)`, `mark_stale(db, cfg)`.
      **Verification:** §3.1 tests green.

## 4. Worker (RED-first)

- [ ] 4.1 Write unit tests in `tests/unit/test_queue_worker.py`
      (new file): a `threading.Event`-based fake pipeline;
      - normal run: `pending` → `running` (one sub-batch) →
        `progress` snapshot after the highwater commit → `done`
      - cancel mid-run: set row `cancelled` between sub-batches →
        current sub-batch completes (all-or-nothing commit order),
        row finishes `cancelled`, remaining sub-batches skipped
      - pipeline exception → row `error`, `last_error` = the
        exception string (never an absolute host path — NFR-13)
      - stale: a running row whose last progress snapshot exceeds
        `queue.stale_after_min` → next `tick()` marks it `error`
      **Verification:** tests fail on current code (module absent).
- [ ] 4.2 Implement `digital_twins/ingest/queue_worker.py` (new):
      `IngestQueueWorker(db, cfg)` — `start()` (daemon thread),
      `stop()`, `tick()` (`mark_stale` → `dequeue` →
      `run_pipeline` with a per-sub-batch `progress` hook appended
      *after* each highwater commit → `finish`). Cancel is checked
      at sub-batch boundaries only; the in-progress sub-batch
      completes fully (019 commit order unchanged).
      **Verification:** §4.1 tests green.

## 5. Enqueue path (FR-4)

- [ ] 5.1 `digital_twins/mcp/dispatch.py`: when `queue.enabled` is
      true, `kb_ingest_folder` / `kb_ingest` call `enqueue` and
      return `{"ok": true, "run_id": "..."}` instead of running
      `run_pipeline` inline. `document_key` = the caller-supplied
      folder path (016) / source name (007) — never a host-resolved
      path (NFR-13). **Verification:** a unit test asserts the
      enqueued row + response shape when the knob is on; the
      §1.2 test (knob off) still green.

- [ ] 5.2 `pipeline.py` (019): add an optional
      `progress_hook` parameter to the sub-batched loop —
      `None` (default) → byte-for-byte unchanged; the worker
      passes one that appends `progress_pcts` after each sub-batch's
      highwater checkpoint. **Verification:** existing 019
      pipeline tests still green with the hook absent.

## 6. MCP tools (FR-5)

- [ ] 6.1 `digital_twins/mcp/registry.py`: three additive tool
      schemas — `kb_ingest_status` `{run_id: str (required)}`;
      `kb_ingest_retry` `{run_id: str (required), item_index: int
      (optional)}`; `kb_ingest_cancel` `{run_id: str (required)}`.
      Descriptions document the `queue_disabled` error code + the
      `progress_pcts` / `items` shapes.
- [ ] 6.2 `digital_twins/mcp/dispatch.py` + the 004 ACL gate:
      `kb_ingest_status` (role `reader`, read-only) →
      `{"ok": true, "status", "attempts", "last_error",
      "progress_pcts", "items"}`; `kb_ingest_retry` (role
      `scheduler`) — INSERTs a new `pending` row for the failed
      item (or the whole run when `item_index` is omitted) with
      `attempts` = old + 1, old row kept; `kb_ingest_cancel`
      (role `scheduler`) — sets the row `cancelled`. All three
      return `{"ok": false, "error": {"code": "queue_disabled"}}`
      when the knob is off. **Verification:** a unit test per tool
      (happy path + `queue_disabled` + the role gate); no existing
      tool body altered.

## 7. Web endpoint (FR-6)

- [ ] 7.1 `GET /api/v1/queue` in `digital_twins/web/app.py`
      (`_dispatch_rest`): bearer-gated (same gate as `kb_search`),
      optional `?status=` query param (default: all rows), newest
      first, default limit 100. Response: `{"ok": true, "rows":
      [{id, source_key, document_key, status, attempts,
      last_error, enqueued_at, started_at, finished_at,
      progress_pcts}]}`. **Verification:** a web-app unit test
      (auth token + knob off → rows empty, `ok: true`; unauthenticated
      → 401; `status=done` filter works).

## 8. Worker boot (FR-3)

- [ ] 8.1 `digital_twins/mcp/http.py` (MCP HTTP server) +
      `digital_twins/web/app.py` (`WebBind` start path): when
      `queue.enabled: true` the `IngestQueueWorker` thread is
      started (daemon, one per process) and stopped on server
      shutdown. The 002 scheduler is **not** touched.
      **Verification:** a unit test asserts the thread starts/stops
      with the knob on and does not start when off.

## 9. Integration + guards

- [ ] 9.1 Integration test: `queue.enabled` on → `kb_ingest_folder`
      returns a `run_id`; poll `kb_ingest_status` to `done`; the
      queue table has one `done` row with a monotonic `progress_pcts`
      array; `kb_ingest_retry` on a `done` row → 404
      `run_not_found`. **Verification:** test green.
- [ ] 9.2 Guards: `python3 -m pytest
      tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → both green (no host paths;
      knob docs in lock-step).
- [ ] 9.3 Full suite: `python3 -m pytest tests/ -q` → all green.

## 10. Semver + release note

- [ ] 10.1 Bump `__version__` (MINOR, 0.15.0 → 0.16.0) + CHANGELOG
      entry (new default-off queue; three new default-off knobs;
      three new MCP tools; one new Web endpoint; byte-for-byte
      unchanged when off).

## 11. Bookkeeping

- [ ] 11.1 OpenSpec artifacts for 022 validated.
- [ ] 11.2 SDD ledger `.superpowers/sdd/022-ingest-queue/progress.md`.

## Verification summary

- `pytest tests/unit/test_queue.py tests/unit/test_queue_worker.py
  tests/unit/test_knob_docs.py -q` → all passed.
- `pytest tests/ -q` → all green.
- `pytest tests/integration/test_portability.py
  tests/unit/test_knob_docs.py -q` → both guards green.
