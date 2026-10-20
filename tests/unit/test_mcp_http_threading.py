"""015 regression: HTTP MCP transport must work with a default conn.

The CLI's ``serve-mcp --transport http`` opens the state DB via
``state.db.connect(state_dir)`` — a plain ``sqlite3.connect(path)`` with
``check_same_thread=True`` — and hands it to ``mcp_http.main(db, ...)``.
Because ``ThreadingHTTPServer`` handles each request on a fresh worker
thread, any db call made from a worker thread raised
``sqlite3.ProgrammingError`` (thread-identity guard) and the handler
died → client saw a reset connection (HTTP 000).

These tests build the handler the **CLI's** way — a conn opened with the
default ``check_same_thread=True`` — so the guard is not masked by a
thread-safe fixture.

NFR-13: no host paths, usernames, or install locations in this file.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import unittest.mock as _mock

import pytest

from digital_twins.accounts import create_account
from digital_twins.state.db import state_db_path
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixture: the CLI's default-conn shape (check_same_thread=True)
# ---------------------------------------------------------------------------

@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    """A migrated state DB opened the **CLI's** way.

    ``state.db.connect()`` returns ``sqlite3.connect(str(path))`` — the
    stdlib default, i.e. ``check_same_thread=True``.  This fixture
    reproduces that shape exactly (same pragmas, same migrate step,
    same service account), so the thread-identity guard is live.
    """
    path = state_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))          # default: check_same_thread=True
    assert conn is not None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    create_account(conn, "system", "pw-system")
    conn.execute("UPDATE accounts SET role='reader' WHERE email='system'")
    conn.commit()
    # A known service token for the authenticator.
    monkeypatch.setenv("DT_SERVICE_TOKEN", "test-service-token-015")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# helpers (mirror test_mcp_transports.py, no thread-safe fixture)
# ---------------------------------------------------------------------------

def _service_token() -> str:
    return os.environ["DT_SERVICE_TOKEN"]


def _build_http_handler(db, service_account_email: str = "system"):
    """Build the handler the CLI's way (default conn, not the fixture's)."""
    from digital_twins.mcp import http
    return http.build_handler(db, service_account_email=service_account_email)


def _http_post_raw(host: str, port: int, request: dict,
                   auth_header: str | None, timeout: float = 5.0):
    """POST /mcp; return ``(status, body)``.

    A handler-thread crash (socket reset) surfaces as an empty body with
    ``status == 0`` (http.client reports the connection was closed).
    """
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    payload = json.dumps(request).encode("utf-8")
    conn.request("POST", "/mcp", body=payload, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    status = getattr(resp, "status", 0)
    conn.close()
    return status, body


def _start_http_server(handler_cls):
    """Start a real ThreadingHTTPServer on an ephemeral port; return it."""
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, host, port


# ---------------------------------------------------------------------------
# T1.1 RED: valid service-token request through the CLI's default conn
# ---------------------------------------------------------------------------

def test_cli_default_conn_service_token_request_completes(cli_db, monkeypatch):
    """A valid service-token POST must return 200 + dispatch payload.

    Pre-fix: the handler thread crashes on the first db call
    (``accounts.get_role`` / the authenticator) with
    ``sqlite3.ProgrammingError`` → the client sees a reset connection
    (status 0 / empty body).  Post-fix: 200 with a JSON body.
    """
    from digital_twins.mcp import dispatch as dispatch_module

    calls = []

    def fake_dispatch(ctx, tool_name, args):
        calls.append((tool_name, ctx.agent_kind))
        return {"ok": True, "tool": tool_name,
                "health": {"qdrant": "ok", "neo4j": "ok"}}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch,
                       raising=False)
    # The http module resolved ``dispatch`` at import time; patch both.
    from digital_twins.mcp import http as http_module
    if hasattr(http_module, "dispatch"):
        monkeypatch.setattr(http_module, "dispatch", fake_dispatch,
                           raising=False)

    handler = _build_http_handler(cli_db, service_account_email="system")
    server, thread, host, port = _start_http_server(handler)
    try:
        status, body = _http_post_raw(
            host, port,
            {"tool": "kb_health", "args": {}},
            auth_header="Bearer " + _service_token())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    # A reset/empty connection (handler-thread crash) must NOT occur.
    assert status != 0, "connection was reset — handler thread crashed " \
                        "(sqlite3.ProgrammingError expected pre-fix)"
    assert status == 200, f"expected 200, got {status}: {body!r}"
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["tool"] == "kb_health"
    # dispatch was actually reached on a worker thread.
    assert calls == [("kb_health", "http")]


# ---------------------------------------------------------------------------
# T1.2 GREEN-concurrent: two concurrent valid POSTs
# ---------------------------------------------------------------------------

def test_cli_default_conn_concurrent_requests_no_corruption(cli_db,
                                                            monkeypatch):
    """Two concurrent valid POSTs both complete; db state is not corrupted.

    Pre-fix: both requests reset (handler-thread crash).  Post-fix: both
    return 200 and the accounts table still resolves the service account's
    role correctly (no interleaved-cursor corruption).
    """
    from digital_twins.mcp import dispatch as dispatch_module

    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_dispatch(ctx, tool_name, args):
        # A deliberate (tiny) window so both threads overlap on the
        # db-touching path; the fix must serialize that section.
        with lock:
            results.append(tool_name)
        return {"ok": True, "tool": tool_name}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch,
                       raising=False)
    from digital_twins.mcp import http as http_module
    if hasattr(http_module, "dispatch"):
        monkeypatch.setattr(http_module, "dispatch", fake_dispatch,
                           raising=False)

    handler = _build_http_handler(cli_db, service_account_email="system")
    server, thread, host, port = _start_http_server(handler)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            status, body = _http_post_raw(
                host, port,
                {"tool": "kb_health", "args": {}},
                auth_header="Bearer " + _service_token(),
                timeout=10.0)
            assert status == 200, f"worker got {status}: {body!r}"
            assert json.loads(body)["ok"] is True
        except Exception as exc:  # noqa: BLE001 — test-scope
            errors.append(exc)

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=15)

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

    assert not errors, f"concurrent requests failed: {errors}"
    assert sorted(results) == ["kb_health", "kb_health"]
    # db still intact: the service account's role resolves after the storm.
    from digital_twins import accounts
    assert accounts.get_role(cli_db, "system") == "reader"
