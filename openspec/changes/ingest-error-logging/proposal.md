# Change: Surface swallowed ingest-run tracebacks in the web server log

## Why
UAT found that when a pipeline run crashes, the web server log shows only
`WARNING` lines; the actual traceback is swallowed by the catch-all `except
Exception` block in `digital_twins/web/app.py::_handle_ingest_run`
(L973-976), which returns `500 "ingest run failed: see server log for
details"` without logging the exception. This makes it hard to diagnose
operational failures (e.g. BUG-01) from the server log alone.

This change adds a `logging.exception` call in the catch-all so the
traceback is recorded in the server log.

## What changes
- In `digital_twins/web/app.py::_handle_ingest_run`, log the exception with
  `logging.exception("web ingest run failed", exc_info=...)` (or equivalent)
  before sending the 500 response.

## Non-goals
- No change to the pipeline's audit behavior (the `failed` row is still
  written by `run_pipeline`'s own `except` block).
- No change to other handlers' error handling (scope is ingest-only).
- No change to the API response shape (still `500` with the same
  `{"error": ...}` body).

## Impact
- Affected code: `digital_twins/web/app.py` (one log line).
- No new dependencies.
