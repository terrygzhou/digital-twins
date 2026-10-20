# Change: Fix the MCP HTTP transport's SQLite threading crash (BUG-02)

## Why
Owner field-test (2026-10-20, `digital-twins-kb` 0.11.3 on the OCI box):
`digital-twins serve-mcp --transport http` serves **no authenticated request
at all**. The first request that resolves through to the db layer crashes the
handler thread (`sqlite3.ProgrammingError: SQLite objects created in a thread
can only be used in that same thread`) and the client sees a reset
connection (curl `HTTP 000`). Only the 401 path (unknown/missing credential)
returns a clean body because it short-circuits before the first db call.
Every authenticated tool (`kb_search`, `kb_chat`, `kb_ingest`, `kb_health`,
`kb_schedule_*`, `kb_run_history`) is unreachable through the HTTP transport.

## What changes
- The HTTP MCP transport re-opens the state DB with
  `check_same_thread=False` for its threaded handlers, mirroring the
  existing `web/server.py` precedent (`_open_same_db`), and serializes
  db-touching request sections under a `threading.Lock`.
- `state/db.py::connect()` semantics are **unchanged** for every other
  caller (scheduler loop, web, stdio). No **BREAKING** changes.
- No new knob, no config change, no new dependency.
- Regression test: a RED-first case that builds the handler the CLI's way
  (default `check_same_thread=True` conn) and POSTs a valid service-token
  request; fails on current code (reset/`ProgrammingError`), passes after
  the fix.

## Capabilities
- **Modified Capabilities**: `mcp-transport` (new capability path
  `mcp-http-transport` under `specs/` — the HTTP MCP transport's
  externally visible behavior contract: authenticated requests complete
  with a well-formed JSON response on a threaded server, including
  concurrent requests. No `openspec/specs/` main spec exists yet for this
  capability, so the delta is ADDED Requirements describing the post-fix
  contract.)

## Impact
- Affected code: `digital_twins/mcp/http.py` (re-opened conn + request
  lock), `tests/unit/test_mcp_transports.py` or new
  `tests/unit/test_mcp_http_threading.py` (regression case).
- Unaffected: `state/db.py`, `mcp/stdio`, `web/server.py` (already
  correct), CLI entry points (the CLI's main-thread conn becomes unused on
  the HTTP path, harmless).
- Semver: PATCH — behavior fix, no new surface, no config change; bumps
  the 0.11.x line (spec-lite slice 015, ledger-first per AGENTS.md).
