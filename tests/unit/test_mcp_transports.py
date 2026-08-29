"""T025 (RED) + T026–T029 (GREEN): MCP stdio + HTTP transports (feature 004, R7).

The transports are *thin* adapters: both call the same
``digital_twins.mcp.dispatch.dispatch(ctx, name, args)`` function, so the
tool set + audit behavior are identical across transports (R7 parity by
construction).

This test asserts:

1. **R7 parity** — given the same MCPContext + tool args, the stdio
   transport and the HTTP transport return *byte-identical* JSON output.
   The test feeds the same request through both handlers and compares the
   serialized response.

2. **Dispatch wiring** — each transport actually calls
   ``digital_twins.mcp.dispatch.dispatch`` (verified by monkeypatching it
   to record the call + return a sentinel).

3. **Auth wiring (HTTP)** — the HTTP transport uses
   ``digital_twins.mcp.auth.mcp_authenticator`` to resolve the caller
   (service token / personal / session → 401 on unknown).

4. **stdio JSON framing** — the stdio transport reads newline-delimited
   JSON from an input stream and writes one JSON object per line to an
   output stream.

5. **CLI serve-mcp** — the ``digital-twins serve-mcp`` subcommand exists
   with ``--transport {stdio,http}`` and ``--port`` options (T028), and
   the mcp extra is declared in ``pyproject.toml`` (T029).

The tests do NOT require the ``mcp`` SDK — the server speaks raw JSON over
stdio/HTTP (per the task brief), so the transport modules must import and
run with only the stdlib + the package's own dependencies.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from io import StringIO

import pytest

from digital_twins.accounts import create_account
from digital_twins.mcp.auth import mcp_authenticator
from digital_twins.mcp.dispatch import dispatch
from digital_twins.mcp.registry import MCPContext
from digital_twins.state.db import state_db_path
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated state DB with accounts + schedules + audit_runs tables.

    ``check_same_thread=False`` is required: the HTTP transport's
    ``ThreadingHTTPServer`` handles requests on a worker thread, and the
    same connection is shared across threads in the test.

    Additionally, a minimal ``system`` account is created so the service
    token path (DT_SERVICE_TOKEN → service-account email) resolves to a
    live role in the accounts table. Without this, the HTTP transport's
    service-token path would 403 on every request (fail closed).
    """
    path = state_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    # Create the service account so the service-token path has a role to
    # resolve. create_account assigns admin to the first row; we want the
    # service account to be a non-admin role (reader) so the transport
    # is not accidentally over-privileged by the fixture.
    create_account(conn, "system", "pw-system")
    conn.execute("UPDATE accounts SET role='reader' WHERE email='system'")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def ctx(db):
    """An MCPContext for a reader account (the simplest real role)."""
    create_account(db, "alice@example.com", "pw-alice")
    db.execute(
        "UPDATE accounts SET role='reader' WHERE email='alice@example.com'")
    db.commit()
    return MCPContext(
        db=db,
        caller_email="alice@example.com",
        caller_role="reader",
        agent_kind="test",
    )


# ---------------------------------------------------------------------------
# R7 parity: both transports return identical JSON for the same input
# ---------------------------------------------------------------------------

def test_r7_stdio_and_http_return_identical_json(db, ctx):
    """Both transports, fed the same request, return byte-identical JSON.

    This is the R7 guarantee: the tool set + audit behavior is the same
    across transports because both delegate to the same ``dispatch``.
    """
    from digital_twins.mcp import stdio, http

    # --- stdio side: feed one JSON request, capture the JSON response line.
    request = {"tool": "kb_schedule_list", "args": {"all_users": False}}
    inp = StringIO(json.dumps(request) + "\n")
    out = StringIO()
    stdio.serve(inp, out, db, service_account_email="system")
    stdio_lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(stdio_lines) == 1
    stdio_response = json.loads(stdio_lines[0])

    # --- HTTP side: POST the same request to a live HTTP handler, capture
    # the JSON response body.
    handler = _build_http_handler(db, service_account_email="system")
    with _http_server(handler) as (host, port):
        body = _http_post(host, port, request,
                          auth_header="Bearer " + _service_token())
        http_response = json.loads(body)

    # R7: the two responses are identical JSON (byte-identical after
    # canonical serialization).
    assert json.dumps(stdio_response, sort_keys=True) == \
        json.dumps(http_response, sort_keys=True)


def test_r7_dispatch_is_called_by_both_transports(db, ctx, monkeypatch):
    """Both transports delegate to digital_twins.mcp.dispatch.dispatch.

    Monkeypatch dispatch to record the call and return a sentinel; verify
    each transport hands the tool name + args through unchanged.
    """
    from digital_twins.mcp import stdio, http
    from digital_twins.mcp import dispatch as dispatch_module

    calls: list[tuple[str, dict]] = []

    def fake_dispatch(ctx, tool_name, args):
        calls.append((tool_name, dict(args)))
        return {"ok": True, "sentinel": "dispatch-was-called"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch,
                        raising=False)
    # The transports import dispatch by reference at module level; patch the
    # attribute on the transport modules too so both paths are observed.
    if hasattr(stdio, "dispatch"):
        monkeypatch.setattr(stdio, "dispatch", fake_dispatch, raising=False)
    if hasattr(http, "dispatch"):
        monkeypatch.setattr(http, "dispatch", fake_dispatch, raising=False)

    request = {"tool": "kb_run_history", "args": {"limit": 5}}

    # stdio
    inp = StringIO(json.dumps(request) + "\n")
    out = StringIO()
    stdio.serve(inp, out, db, service_account_email="system")
    stdio_response = json.loads(
        [ln for ln in out.getvalue().splitlines() if ln.strip()][0])
    assert stdio_response["ok"] is True
    assert stdio_response["sentinel"] == "dispatch-was-called"
    assert ("kb_run_history", {"limit": 5}) in calls

    calls.clear()

    # http
    handler = _build_http_handler(db, service_account_email="system")
    with _http_server(handler) as (host, port):
        body = _http_post(host, port, request,
                          auth_header="Bearer " + _service_token())
        http_response = json.loads(body)
    assert http_response["ok"] is True
    assert http_response["sentinel"] == "dispatch-was-called"
    assert ("kb_run_history", {"limit": 5}) in calls


# ---------------------------------------------------------------------------
# stdio transport: JSON framing
# ---------------------------------------------------------------------------

def test_stdio_reads_json_lines_and_writes_json_lines(db, ctx, monkeypatch):
    """The stdio transport reads NDJSON requests and writes NDJSON responses.

    Multiple requests in one session → one response line each, in order.
    """
    from digital_twins.mcp import stdio
    from digital_twins.mcp import dispatch as dispatch_module

    order: list[str] = []

    def fake_dispatch(ctx, tool_name, args):
        order.append(tool_name)
        return {"ok": True, "echo": tool_name, "args": dict(args)}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch,
                        raising=False)
    if hasattr(stdio, "dispatch"):
        monkeypatch.setattr(stdio, "dispatch", fake_dispatch, raising=False)

    reqs = [
        {"tool": "kb_schedule_list", "args": {}},
        {"tool": "kb_run_history", "args": {"limit": 3}},
    ]
    inp = StringIO("".join(json.dumps(r) + "\n" for r in reqs))
    out = StringIO()
    stdio.serve(inp, out, db, service_account_email="system")

    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert [json.loads(ln)["echo"] for ln in lines] == [
        "kb_schedule_list", "kb_run_history"]
    assert [json.loads(ln)["args"] for ln in lines] == [
        {}, {"limit": 3}]


def test_stdio_unknown_tool_returns_internal_error(db, ctx):
    """An unknown tool name is mapped to the dispatch error shape."""
    from digital_twins.mcp import stdio

    inp = StringIO(json.dumps(
        {"tool": "kb_totally_bogus", "args": {}}) + "\n")
    out = StringIO()
    stdio.serve(inp, out, db, service_account_email="system")
    line = [ln for ln in out.getvalue().splitlines() if ln.strip()][0]
    resp = json.loads(line)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "internal_error"


# ---------------------------------------------------------------------------
# HTTP transport: auth + dispatch
# ---------------------------------------------------------------------------

def test_http_unknown_credential_is_401(db, monkeypatch):
    """A request with no valid credential → 401 (mcp_authenticator gate)."""
    from digital_twins.mcp import http
    from digital_twins.mcp import dispatch as dispatch_module

    # dispatch must NOT be reached when auth fails.
    called = []
    monkeypatch.setattr(
        dispatch_module, "dispatch",
        lambda ctx, name, args: called.append(1) or {"ok": True},
        raising=False)
    if hasattr(http, "dispatch"):
        monkeypatch.setattr(http, "dispatch",
                            lambda ctx, name, args: called.append(1)
                            or {"ok": True}, raising=False)

    handler = _build_http_handler(db, service_account_email="system")
    with _http_server(handler) as (host, port):
        status, body = _http_post_raw(
            host, port,
            {"tool": "kb_schedule_list", "args": {}},
            auth_header=None)
    assert status == 401
    assert called == []  # dispatch never reached


def test_http_service_token_reaches_dispatch(db, monkeypatch):
    """A valid service token → 200, dispatch called with the service email."""
    from digital_twins.mcp import http
    from digital_twins.mcp import dispatch as dispatch_module

    seen_ctx = []

    def fake_dispatch(ctx, tool_name, args):
        seen_ctx.append(ctx)
        return {"ok": True, "tool": tool_name}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch,
                        raising=False)
    if hasattr(http, "dispatch"):
        monkeypatch.setattr(http, "dispatch", fake_dispatch, raising=False)

    handler = _build_http_handler(db, service_account_email="system")
    with _http_server(handler) as (host, port):
        status, body = _http_post_raw(
            host, port,
            {"tool": "kb_schedule_list", "args": {}},
            auth_header="Bearer " + _service_token())
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert len(seen_ctx) == 1
    assert seen_ctx[0].caller_email == "system"
    assert seen_ctx[0].agent_kind == "http"  # the HTTP transport declares itself


# ---------------------------------------------------------------------------
# CLI: serve-mcp subcommand (T028) + mcp extra (T029)
# ---------------------------------------------------------------------------

def test_serve_mcp_cli_command_exists():
    """digital-twins serve-mcp --transport {stdio,http} [--port N] exists."""
    from click.testing import CliRunner
    from digital_twins.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["serve-mcp", "--help"])
    assert res.exit_code == 0
    assert "--transport" in res.output
    assert "--port" in res.output
    assert "stdio" in res.output
    assert "http" in res.output


def test_serve_mcp_rejects_unknown_transport():
    """--transport with an invalid value exits non-zero (click Choice)."""
    from click.testing import CliRunner
    from digital_twins.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["serve-mcp", "--transport", "carrier-pigeon"])
    assert res.exit_code != 0


def test_pyproject_declares_mcp_extra():
    """pyproject.toml declares an optional `mcp` extra (T029)."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    # The optional-dependencies table must name an `mcp` extra that pulls in
    # the mcp SDK (>=1.2,<2 per the task brief).
    assert "mcp" in text
    # The extra must be under [project.optional-dependencies].
    assert "[project.optional-dependencies]" in text
    # The version pin matches the brief: mcp>=1.2,<2.
    assert "mcp>=1.2" in text


def test_mcp_main_module_imports():
    """digital_twins.mcp is importable as a package (python -m support)."""
    import digital_twins.mcp  # noqa: F401


# ---------------------------------------------------------------------------
# test helpers
# ---------------------------------------------------------------------------

def _service_token() -> str:
    """Set + return a stable DT_SERVICE_TOKEN for the test."""
    import os
    os.environ["DT_SERVICE_TOKEN"] = "test-service-token"
    return "test-service-token"


def _build_http_handler(db, service_account_email: str = "system"):
    """Build the HTTP request handler the test server will serve.

    Delegates to the production http module's handler-builder so the test
    exercises the real code path (auth + dispatch wiring).
    """
    from digital_twins.mcp import http
    return http.build_handler(db, service_account_email=service_account_email)


@contextmanager
def _http_server(handler_cls):
    """Run a ThreadingHTTPServer on 127.0.0.1:0 with the given handler.

    Yields ``(host, port)``; shuts the server down on exit. The handler
    must be a ``BaseHTTPRequestHandler`` subclass (typically the one
    returned by ``http.build_handler``).
    """
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _http_post(host, port, request: dict, auth_header: str | None) -> str:
    """POST a JSON request; return the response body (raises on non-200)."""
    status, body = _http_post_raw(host, port, request, auth_header=auth_header)
    if status != 200:
        raise AssertionError(f"expected 200, got {status}: {body!r}")
    return body


def _http_post_raw(host, port, request: dict, auth_header: str | None):
    """POST a JSON request; return ``(status, body)`` without raising."""
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    payload = json.dumps(request).encode("utf-8")
    conn.request("POST", "/mcp", body=payload, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body
