"""MCP HTTP transport (feature 004, R7, T027).

A minimal HTTP server that accepts ``POST /mcp`` with a JSON body::

    {"tool": "<tool_name>", "args": {...}}

and returns the exact dict returned by
:func:`digital_twins.mcp.dispatch.dispatch` as the JSON response.

The transport is *thin*: all logic (role-gate, owner-scope, tool bodies,
audit) lives in ``dispatch`` — this module only handles HTTP framing,
authentication, and I/O. R7 parity with the stdio transport (T026) holds
by construction: both delegate to the same ``dispatch``.

Authentication
--------------
Per-request, via the ``Authorization: Bearer <token>`` header, using
:func:`digital_twins.mcp.auth.mcp_authenticator` (the same credential
order as 003's ``cli._auth_checker``): service token → personal token →
session token. An unknown / missing credential → HTTP 401; a
known-but-dead credential → HTTP 403 (fail closed, mirroring the
``(False, <reason>)`` authenticator return).

The ``MCPContext.agent_kind`` is set to ``"http"`` so the audit row's
``per_source_counts`` can attribute the request to the HTTP transport.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import accounts as _accounts
from . import dispatch as _dispatch
from .auth import mcp_authenticator
from .registry import MCPContext
from ..config import loader as _loader


# ---------------------------------------------------------------------------
# thread-safe db (015 / BUG-02)
# ---------------------------------------------------------------------------

def _open_thread_safe_db(db, check_same_thread: bool = False):
    """Open a new connection to the same SQLite file as ``db``.

    Mirrors ``web/server.py::_open_same_db``: the HTTP MCP transport is a
    ``ThreadingHTTPServer`` (fresh worker thread per request), but the
    CLI's ``serve-mcp`` opens the state DB on the main thread with the
    stdlib default ``check_same_thread=True``.  Any db call made from a
    worker thread then raises ``sqlite3.ProgrammingError`` (the
    thread-identity guard) and the handler dies with a reset connection
    (015 defect).  Re-opening the same path with
    ``check_same_thread=False`` keeps the conn usable from worker
    threads; the ``state/db.py`` connection semantics are unchanged for
    every other caller.

    In-memory databases (no file) cannot be re-opened, so the original
    connection is returned as-is.
    """
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        path = row[2] if row and row[2] else None
    except Exception:
        path = None

    if path is None or path == ":memory:":
        return db

    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    # Enforce FKs on the secondary connection too (sqlite3.connect
    # defaults foreign_keys=OFF), matching state.db.connect().
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------

def _build_handler_cls(db, service_account_email: str,
                       dispatch: Any = None,
                       config: Any = None,
                       db_lock: Any = None) -> type:
    """Build a ``BaseHTTPRequestHandler`` subclass wired to ``db`` + ``dispatch``.

    ``dispatch`` is injectable for tests; defaults to
    :func:`digital_twins.mcp.dispatch.dispatch`. The returned class is
    suitable for ``ThreadingHTTPServer``.

    ``config`` (feature 007, 007-R6c): the loaded config dict, threaded
    into the handler's per-request ``MCPContext`` so the KB tool bodies
    can read it. The programmatic seam defaults to ``None``; the CLI
    entry point (:func:`main`) always loads it via the config layer
    first (007-R6c) so a live server never dispatches with a ``None``
    config.

    ``db_lock`` (015): an optional ``threading.Lock`` serializing the
    db-touching sections of ``do_POST`` (auth verify + role resolution
    + dispatch audit write) across worker threads.  ``check_same_thread=
    False`` only disables the thread-*identity* guard; it does not make
    concurrent use of one ``sqlite3.Connection`` safe — the lock exists
    for exactly this (same rationale as ``web/server.py``).  The lock is
    scoped to the db-touching sections, NOT the KB tool bodies'
    Qdrant/Neo4j/LLM I/O, so the threaded server keeps its concurrency
    benefit.  Defaults to ``None`` (no lock) so single-connection,
    non-threaded callers (stdio tests, direct handler construction) are
    unaffected.
    """
    if dispatch is None:
        dispatch = _dispatch.dispatch
    authenticator = mcp_authenticator(db, service_account_email)

    class Handler(BaseHTTPRequestHandler):
        # Silence the default access log (tests do not want it on stderr).
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            pass

        # --- HTTP methods ------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            """GET → 405 (this server only accepts POST /mcp in v1).

            SSE / streaming is out of scope for the 004 transport slice
            (research D2: v1 is request/response JSON; a follow-up slice
            adds the SSE endpoint).
            """
            self._send(405, {"ok": False,
                             "error": {"code": "method_not_allowed",
                                       "message": "use POST /mcp"}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/mcp":
                self._send(404, {"ok": False,
                                 "error": {"code": "not_found",
                                           "message": f"unknown path: {self.path}"}})
                return

            # 015: the db-touching sections (auth verify + role
            # resolution) are serialized under db_lock when one is
            # supplied.  check_same_thread=False only disables the
            # thread-identity guard; it does not make concurrent use of
            # one sqlite3.Connection safe — the lock covers that (same
            # rationale as web/server.py).  The lock is released before
            # dispatch so the KB tool bodies' external I/O (Qdrant /
            # Neo4j / LLM) can overlap across worker threads.
            with db_lock if db_lock is not None else nullcontext():
                # --- auth gate (mcp_authenticator) -----------------------
                ok, email_or_reason = authenticator(self.headers)
                if not ok:
                    # 401 for an unknown / missing credential
                    # (email_or_reason is None); 403 for a known-but-dead
                    # credential (a reason string is present).
                    status, payload = self._auth_reject(email_or_reason)
                    self._send(status, payload)
                    return

                # --- role resolution (accounts.get_role) -----------------
                caller_role = _accounts.get_role(db, email_or_reason)
                if caller_role is None:
                    # Race: account deleted between token verify and role
                    # resolve.  Fail closed -> 403.
                    self._send(403, {"ok": False,
                                     "error": {"code": "forbidden",
                                               "message": "account no longer exists"}})
                    return

            # --- read the JSON body --------------------------------------
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                request = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError as exc:
                self._send(400, {"ok": False,
                                 "error": {"code": "bad_request",
                                           "message": f"malformed JSON body: {exc}"}})
                return
            if not isinstance(request, dict):
                self._send(400, {"ok": False,
                                 "error": {"code": "bad_request",
                                           "message": "body must be a JSON object"}})
                return

            # --- dispatch (external I/O runs WITHOUT the db lock) --------
            # 007-R6c: thread the loaded config into MCPContext so the KB
            # tool bodies (kb_search / kb_chat / kb_ingest / kb_health)
            # can read it.  The programmatic seam defaults to None; the
            # live transport (:func:`main`) always loads it first.
            ctx = MCPContext(
                db=db,
                caller_email=email_or_reason,
                caller_role=caller_role,
                agent_kind="http",
                config=config,
            )
            response = dispatch(ctx, request.get("tool", ""),
                                request.get("args") or {})
            self._send(200, response)

        # --- response writer --------------------------------------------
        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth_reject(self, email_or_reason) -> tuple:
            """Status + body for a rejected credential (015 lock-path helper)."""
            if email_or_reason is None:
                return 401, {"ok": False, "error": {"code": "unauthorized",
                                                     "message": "authentication failed"}}
            return 403, {"ok": False, "error": {"code": "forbidden",
                                                 "message": email_or_reason}}

    return Handler


def build_handler(db, service_account_email: str = "system",
                  dispatch: Any = None,
                  config: Any = None) -> type:
    """Public builder: return the request-handler class for ``db``.

    This is the seam the tests + the CLI use: the handler is constructed
    against the open state DB and the dispatch callable, and ready to be
    handed to a ``ThreadingHTTPServer``.

    ``config`` (feature 007, 007-R6c): the loaded config dict, threaded
    into the handler's per-request ``MCPContext``. Defaults to ``None``;
    the CLI entry point (:func:`main`) loads it first.

    015 / BUG-02: this seam is safe to serve from a
    ``ThreadingHTTPServer`` — it re-opens the conn
    (``check_same_thread=False``) and serializes the db-touching
    request sections under a ``threading.Lock``.  The caller's ``db``
    is not mutated; the handler owns a re-opened connection to the
    same file.  In-memory conns cannot be shared across threads, so
    they are returned as-is and carry no lock (pre-015
    single-thread contract preserved).
    """
    # 015 / BUG-02: re-open the same file thread-safe + serialize the
    # db-touching request sections under a lock (mirror of
    # web/server.py).  In-memory conns come back as-is (no lock) —
    # they cannot be shared across threads anyway.
    thread_safe_db = _open_thread_safe_db(db, check_same_thread=False)
    db_lock = threading.Lock()
    return _build_handler_cls(thread_safe_db, service_account_email,
                              dispatch, config=config, db_lock=db_lock)


# ---------------------------------------------------------------------------
# server entry point
# ---------------------------------------------------------------------------

def serve(db, host: str = "127.0.0.1", port: int = 8770,
          service_account_email: str = "system",
          dispatch: Any = None,
          config: Any = None) -> ThreadingHTTPServer:
    """Start the HTTP MCP server on ``(host, port)``.

    Returns the ``ThreadingHTTPServer`` (call ``.serve_forever()`` to run;
    the CLI's ``serve-mcp`` does this). ``dispatch`` is injectable for
    tests; defaults to :func:`digital_twins.mcp.dispatch.dispatch`.

    015 / BUG-02: the handler always works against a re-opened
    ``check_same_thread=False`` connection to the same SQLite file
    (in-memory conns are returned as-is) with a ``threading.Lock``
    held across the db-touching sections of every request — see
    ``_build_handler_cls`` (mirror of ``web/server.py``).  The caller's
    ``db`` is not mutated; the server owns its own connection.

    ``config`` (feature 007, 007-R6c): the loaded config dict, threaded
    into the handler's per-request ``MCPContext``. Defaults to ``None``;
    the CLI entry point (:func:`main`) loads it first.
    """
    # 015 / BUG-02: the CLI hands us the main-thread conn (default
    # check_same_thread=True); build_handler re-opens the same file
    # thread-safe and serializes the db-touching request sections under
    # a lock (mirror of web/server.py).  The CLI's original conn stays
    # usable; the server owns its own connection.
    handler = build_handler(db, service_account_email, dispatch,
                            config=config)
    server = ThreadingHTTPServer((host, port), handler)
    return server


def main(db, service_account_email: str = "system", port: int = 8770,
         config: Any = None) -> None:
    """Run the HTTP MCP server until interrupted.

    ``config`` (feature 007, 007-R6c): the loaded config dict. When
    omitted, ``main`` loads it via the config layer
    (:func:`digital_twins.config.loader.load`) and threads it into
    :func:`serve` → the handler's ``MCPContext`` so the KB tool bodies
    can read it.
    """
    if config is None:
        config = _loader.load()
    server = serve(db, port=port,
                   service_account_email=service_account_email,
                   config=config)
    try:
        server.serve_forever()
    finally:
        server.server_close()
