# Design: mcp-http-sqlite-threading

## Context

`digital-twins serve-mcp --transport http` starts a `ThreadingHTTPServer`
(`digital_twins/mcp/http.py`). The CLI (`cli.serve_mcp`) opens the state DB
once on the main thread via `state.db.connect(state_dir)` — a default
`sqlite3.connect(path)` with `check_same_thread=True` — and hands that
connection to `mcp_http.main(db, ...)` → `serve(db, ...)` → the threaded
server.

Every HTTP request is handled on a fresh worker thread. Any request that
reaches a db call — the service/personal/session token verify,
`accounts.get_role(db, ...)` (http.py:123), or the dispatch audit insert —
runs on a worker thread that is not the thread that created the connection,
so sqlite3 raises `ProgrammingError` and the handler thread dies with a
socket reset (client sees `HTTP 000`).

The only request shape that returns a clean body is the 401 path
(unknown / missing auth), because it short-circuits before the first db call.

## Root cause (confirmed on-box, 0.11.3)

Server log under `ThreadingHTTPServer`:

```
digital_twins/mcp/http.py line 123, in do_POST
    caller_role = _accounts.get_role(db, email_or_reason)
File ".../digital_twins/accounts.py", line 181, in get_role
    row = db.execute(...)
sqlite3.ProgrammingError: SQLite objects created in a thread can only be
used in that same thread. The object was created in thread id ...
and this is thread id ...   # a ThreadingHTTPServer worker thread
```

Minimal 2-thread proof: `sqlite3.connect(path)` with default
`check_same_thread=True`, then `accounts.get_role(conn, ...)` from a second
thread → `ProgrammingError`; the same call with `check_same_thread=False`
→ returns `'admin'`.

## Why the existing tests missed it

`tests/unit/test_mcp_transports.py` builds its `db` fixture with
`check_same_thread=False` (the fixture docstring says so explicitly) and
drives the handler via `_build_http_handler(db, ...)` + `_http_server` +
`_http_post`. Because the fixture connection is already thread-safe, the
same-thread guard never fires under the test → tests stay green while the
real CLI path (default conn) crashes. The bug only manifests when the
connection is the default `check_same_thread=True` conn that
`state.db.connect()` returns.

## Precedent: web/server.py

`digital_twins/web/server.py` (feature 003) is also a `ThreadingHTTPServer`
but already re-opens its db with `check_same_thread=False`
(`_open_same_db`, web/server.py:257) and serializes requests under a
`threading.Lock` (documented in the class docstring). The stdio transport
is single-threaded, so it is unaffected. The MCP HTTP transport is simply
the one place that skipped that treatment.

## Chosen approach: re-open + lock, contained to mcp/http.py

Mirror the web-server precedent exactly; keep `state/db.py` semantics
untouched for every other caller (scheduler loop, web, stdio):

1. **Re-open the conn thread-safe.** In `serve()` / `main()`, obtain a conn
   for the handler by re-opening the same db path with
   `check_same_thread=False` — the exact pattern of
   `web/server.py::_open_same_db`. Use that connection for the
   authenticator closure, `accounts.get_role(db, ...)`, and the
   `MCPContext(db=...)` that feeds `dispatch` (the audit insert). The
   CLI's original main-thread conn is then simply unused on the HTTP path.

2. **Serialize the shared conn.** Add a `threading.Lock` and hold it
   across the db-touching sections of `do_POST` (auth verify +
   `get_role` + the dispatch audit write). Rationale:
   `check_same_thread=False` only disables the thread-identity guard; it
   does not make concurrent use of one `sqlite3.Connection` safe — the
   web server's lock exists for exactly this.

   **Lock scope decision**: hold the lock across the db-touching sections
   only (auth verify, `get_role`, audit insert), NOT across the KB tool
   bodies' Qdrant/Neo4j/LLM I/O. The web server holds the lock across the
   whole request, which is acceptable there because its handlers are fast;
   the MCP KB tools do network I/O and would block each other.

## Alternative (rejected): flip state/db.py default

Flipping `state/db.py::connect()` to default `check_same_thread=False`
would change the connection semantics for every caller (scheduler, web,
stdio, tests) and deviates from the project's established convention that
a threaded HTTP server re-opens its own conn (web/server.py). The
contained fix matches the precedent and keeps blast radius to one transport.

## Guardrails

- NFR-13: the fix is library code only — no host paths, usernames, or
  install locations introduced.
- No new config knob; no config surface change.
- Semver: PATCH — behavior fix, no new surface, no breaking change.

## Files touched

- `digital_twins/mcp/http.py` — re-open conn `check_same_thread=False` +
  request lock (mirror `web/server.py`).
- `tests/unit/test_mcp_transports.py` (or new
  `tests/unit/test_mcp_http_threading.py`) — regression case.

## Open questions

None — the approach mirrors the existing `web/server.py` precedent and the
design decisions are settled in the defect doc
(`docs/015-http-mcp-sqlite-threading.md`).
