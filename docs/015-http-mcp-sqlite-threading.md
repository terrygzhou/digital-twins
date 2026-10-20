# 015 — http-mcp sqlite threading bug (spec-lite, ledger-first slice)

Opened 2026-10-20 from owner field-test on the OCI box
(terry-ubuntu-keyed, `digital-twins-kb` 0.11.3 installed in
`~/digital-twins-venv`). **This file is a plan + defect record only; it does
not implement the fix.** Per AGENTS.md the canonical home for a numbered slice
is `specs/015-http-mcp-sqlite-threading/` — this copy lives under `docs/`
at the owner's direction and is drop-in-movable to `specs/015-…/` (rename the
folder + add a `tasks.md`).

## Defect (evidence captured on-box, 2026-10-20)

`digital-twins serve-mcp --transport http` serves **no authenticated request
at all**: the first request that resolves through to the db layer crashes the
handler thread and the client sees a reset connection (curl `HTTP 000`). The
only request shape that returns a clean JSON body is an *unknown / missing*
credential (the `401` path), because that path short-circuits before it ever
touches the db.

Server log (reproduced under `ThreadingHTTPServer`):

```
digital_twins/mcp/http.py line 123, in do_POST
    caller_role = _accounts.get_role(db, email_or_reason)
  File ".../digital_twins/accounts.py", line 181, in get_role
    row = db.execute(...)
sqlite3.ProgrammingError: SQLite objects created in a thread can only be
used in that same thread. The object was created in thread id ...
and this is thread id ...   # a ThreadingHTTPServer worker thread
```

Minimal 2-thread proof (no server needed): `sqlite3.connect(path)` with the
default `check_same_thread=True`, then `accounts.get_role(conn, …)` from a
*second* thread → `ProgrammingError`; the same call with
`check_same_thread=False` → returns `'admin'`.

## Root cause

- `cli.serve_mcp` (HTTP branch) does `db = connect(state_dir)` — a default
  `sqlite3.connect(path)` with **`check_same_thread=True`** — and hands that
  conn to `mcp_http.main(db, …)` → `serve(db, …)` → `ThreadingHTTPServer`.
- `digital_twins/mcp/http.py` is a `ThreadingHTTPServer`: every request is
  handled on a **fresh worker thread**, but all handlers share that one
  main-thread-created conn.
- On any request that reaches a db call — the service/personal/session token
  verify, `accounts.get_role(db, …)` (http.py:123), or the dispatch audit
  insert — the worker thread is not the thread that created the conn, so
  sqlite3 raises `ProgrammingError` before a response is written → handler
  dies → socket reset (`000`).
- The `401` path (unknown / no `Authorization`) returns *before* the first db
  call, which is why it is the only "working" response and why a superficial
  test can look fine.

## Why the existing tests missed it

`tests/unit/test_mcp_transports.py` builds its `db` fixture with
**`check_same_thread=False`** (the fixture docstring says so explicitly) and
drives the handler via `_build_http_handler(db, …)` + `_http_server` +
`_http_post`. Because the fixture conn is already thread-safe, the
same-thread guard never fires under the test → tests stay green while the
real CLI path (default conn) crashes. The bug only manifests when the conn is
the **default** `check_same_thread=True` conn that `state.db.connect()`
returns.

## Blast radius (confirmed)

- **Affected:** only the HTTP MCP transport driven through the CLI
  (`serve-mcp --transport http`). Every authenticated tool
  (`kb_search`, `kb_chat`, `kb_ingest`, `kb_health`, all `kb_schedule_*`,
  `kb_run_history`) is unreachable.
- **Unaffected — and the precedent:** `digital_twins/web/server.py` (003) is
  *also* a `ThreadingHTTPServer` but already re-opens its db with
  `check_same_thread=False` (`_open_same_db`, web/server.py:257) and
  serializes requests under a `threading.Lock` (documented in the class
  docstring). The `stdio` transport is single-threaded, so it is unaffected.
  The MCP HTTP transport is simply the one place that skipped that treatment.

## Fix (recommended — mirror the web-server precedent; narrow + low-risk)

Contain the change to `digital_twins/mcp/http.py` so `state/db.py` semantics
are untouched for every other caller (scheduler loop, web, stdio):

1. **Re-open the conn thread-safe.** In `serve()` / `main()`, obtain a conn
   for the handler by re-opening the same db path with
   `check_same_thread=False` — the exact pattern of
   `web/server.py::_open_same_db`. Use that conn for the authenticator
   closure, `accounts.get_role(db, …)`, and the `MCPContext(db=…)` that feeds
   `dispatch` (the audit insert). The CLI's original main-thread conn is then
   simply unused on the HTTP path.
2. **Serialize the shared conn.** Add a `threading.Lock` and hold it across
   the db-touching sections of `do_POST` (auth verify + `get_role` + the
   dispatch audit write). Rationale: `check_same_thread=False` only disables
   the thread-*identity* guard; it does not make concurrent use of one
   `sqlite3.Connection` safe — the web server's lock exists for exactly this.
   Scope the lock to the db-touching sections, **not** the KB tool bodies'
   Qdrant/Neo4j I/O, so the threaded server keeps its concurrency benefit
   (the web server holds the lock across the whole request; that is
   acceptable there because its handlers are fast, but the MCP KB tools do
   network I/O).

Alternative (rejected): flip `state/db.py::connect()` to default
`check_same_thread=False`. Rejected because it changes the connection
semantics for *every* caller (scheduler, web, stdio, tests) and deviates from
the project's established convention that **a threaded HTTP server re-opens
its own conn** (web/server.py). The contained fix matches the precedent and
keeps blast radius to one transport.

Guardrail (NFR-13): the fix is library code only — no host paths, usernames,
or install locations introduced.

## Regression test (RED-first, per constitution Principle III)

Add to `tests/unit/test_mcp_transports.py` (or a new
`tests/unit/test_mcp_http_threading.py`):

- Build the handler the **CLI's** way: a conn opened with the **default**
  `check_same_thread=True` (i.e. `state.db.connect()` shape — do *not* reuse
  the thread-safe `db` fixture, or the guard is masked again), wire it into
  `mcp_http` exactly as `cli.serve_mcp` does (`_build_http_handler` + the
  real `ThreadingHTTPServer` on an ephemeral port).
- POST a **valid** service-token request (`Authorization: Bearer <DT_SERVICE_TOKEN>`,
  e.g. `kb_health`) from a client socket.
- **RED (pre-fix):** the response is a reset/empty body (client `HTTP 000`),
  and/or the server log carries the `ProgrammingError` traceback.
- **GREEN (post-fix):** client receives `HTTP 200` with the tool payload;
  `kb_health`/a schedule tool returns real data; concurrent valid requests do
  not interleave-corrupt the conn.

## Semver ruling

PATCH (behavior fix, no new surface, no config change, no breaking change).
No new knob. Follows the 010/012 hotfix-slice ruling style; bump the line at
next release (current line 0.11.3).

## Files touched (by the fix, not this plan)

- `digital_twins/mcp/http.py` — re-open conn `check_same_thread=False` +
  request lock (mirror `web/server.py`).
- `tests/unit/test_mcp_transports.py` (or new `test_mcp_http_threading.py`)
  — regression case above.

## Status

Open — planned, not implemented. Evidence + root cause verified on-box
2026-10-20 (0.11.3). Next step: file as `specs/015-http-mcp-sqlite-threading/`
(`plan.md` = this doc, `tasks.md` = the fix + regression-test checkboxes),
write the RED test first (constitution Test-First gate), then the
`mcp/http.py` fix, run the suite + guards (`test_portability`, `test_knob_docs`),
bump the 0.11.x line.
