# Tasks: mcp-http-sqlite-threading

## 1. Regression test (RED-first, constitution Principle III)

- [x] 1.1 Add a RED regression test in `tests/unit/test_mcp_http_threading.py`
      (new file) that builds the handler the **CLI's** way: a conn opened
      with the default `check_same_thread=True` (i.e. the shape
      `state.db.connect()` produces — do NOT reuse the thread-safe `db`
      fixture from `test_mcp_transports.py`, or the guard is masked again),
      wire it into `mcp_http._build_handler` + a real
      `ThreadingHTTPServer` on an ephemeral port, then POST a valid
      service-token request (`Authorization: Bearer <token>`, e.g.
      `kb_health`). **RED (pre-fix):** the response is a reset/empty body
      (client `HTTP 000`) and/or the server log carries the
      `ProgrammingError` traceback. **Verification:** test fails on
      current code with a `sqlite3.ProgrammingError` or empty response.
      No host paths or install locations in the test (NFR-13).

- [x] 1.2 Add a GREEN-concurrent test in the same file: two concurrent
      valid service-token POSTs against the running threaded server both
      complete with HTTP 200 and no interleaved/corrupt db state.
      **Verification:** test fails on current code (same ProgrammingError)
      and passes after the fix in §2.

## 2. Fix: re-open conn + request lock in mcp/http.py

- [x] 2.1 In `digital_twins/mcp/http.py`, add a `_open_thread_safe_db(db)`
      helper that mirrors `web/server.py::_open_same_db`:
      re-open the same db file with `check_same_thread=False` (return the
      original conn unchanged for in-memory / `:memory:` paths). Call it
      in `serve()` / `main()` before building the handler, so the handler
      receives the thread-safe connection for the authenticator closure,
      `accounts.get_role(db, ...)`, and `MCPContext(db=...)`.
      **Verification:** `_open_thread_safe_db` unit test (a conn opened
      with default `check_same_thread=True` is re-opened and the new
      conn's `check_same_thread` attribute is `False`).

- [x] 2.2 Add a `threading.Lock` to the HTTP server (or to the handler
      class built by `_build_handler_cls`) and hold it across the
      db-touching sections of `do_POST`: auth verify,
      `accounts.get_role(db, ...)`, and the dispatch audit write.
      **Scope:** the lock is held across db-touching sections only — NOT
      across the KB tool bodies' Qdrant/Neo4j/LLM network I/O — so the
      threaded server retains its concurrency benefit for external
      service calls. **Verification:** GREEN — the two tests from §1 now
      pass; no `ProgrammingError` in the server log under concurrent load.

## 3. Standalone guards

- [x] 3.1 Confirm `tests/integration/test_portability.py` (T006) remains
      green: `python3 -m pytest tests/integration/test_portability.py -q`.
      No host paths, usernames, or install locations introduced by the
      fix (NFR-13 / BR-11).

- [x] 3.2 Confirm `tests/unit/test_knob_docs.py` (T027) remains green:
      `python3 -m pytest tests/unit/test_knob_docs.py -q`. No new knob
      was added; the knob docs are unchanged.

- [x] 3.3 Run the full unit + integration suite:
      `python3 -m pytest tests/ -q`. All tests green. The two new
      regression tests from §1 are included.

## 4. Semver + release note

- [x] 4.1 Bump the version line to 0.11.x (PATCH — behavior fix, no new
      surface, no config change, no breaking change). Update
      `digital_twins/__init__.py` `__version__`. Follow the 010/012
      hotfix-slice ruling style.

- [x] 4.2 Add a one-line release note under the 0.11.x entry in
      `CHANGELOG.md` (or the project's release-notes file): "Fixed: MCP
      HTTP transport now serves authenticated requests under a threaded
      server (BUG-02)."

## 5. SDD ledger sync (per AGENTS.md)

- [x] 5.1 Create `.superpowers/sdd/015-http-mcp-sqlite-threading/` with
      `progress.md` recording: defect evidence (from
      `docs/015-http-mcp-sqlite-threading.md`), RED-first test outcome,
      fix details, GREEN test outcome, guard results, commit hash.

- [x] 5.2 Create `specs/015-http-mcp-sqlite-threading/` as the canonical
      home for the spec-lite slice: `plan.md` = content of
      `docs/015-http-mcp-sqlite-threading.md` (the defect doc, unchanged),
      `tasks.md` = this file's task list (checkboxes in sync with commits).

## Verification summary

- `python3 -m pytest tests/unit/test_mcp_http_threading.py -q` →
  2 passed (after §2 fix).
- `python3 -m pytest tests/ -q` → all green.
- `python3 -m pytest tests/integration/test_portability.py
  tests/unit/test_knob_docs.py -q` → both guards green.
