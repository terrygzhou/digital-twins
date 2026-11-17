"""025 T019: per-tenant service-token authentication through the MCP
http transport.

Exercises the type-branching logic in
``digital_twins.mcp.http._build_handler_cls`` / ``do_POST`` that
inspects the 2nd element of the ``mcp_authenticator`` tuple and maps
it to ``MCPContext`` fields:

* ``(True, <int>)`` — per-tenant service token; ``caller_tenant_id``
  is the issuing tenant id, ``caller_email`` is ``None``, and
  ``caller_role`` is synthesized to ``install-admin`` (tenant id 1)
  or ``tenant-admin`` (all others).
* ``(True, <str>)`` — personal / session / shared service token;
  ``caller_email`` is the account email, ``caller_tenant_id`` is
  ``None``, and ``caller_role`` is resolved via
  ``accounts.get_role`` (fail-closed 403 when the account no longer
  exists).

The handler under test is exercised through the public
``build_handler`` seam with a fake ``http.server`` request object so
that no real socket I/O happens; the response is captured by
overriding ``_send``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from typing import Any
from unittest import mock

import pytest

from digital_twins.mcp import http as mcp_http
from digital_twins.mcp.registry import MCPContext
from digital_twins.state import db as state_db
from digital_twins import accounts as _accounts


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_db_conn(tmp_path: Any) -> sqlite3.Connection:
    """A migrated state db (file-based, so build_handler's
    ``_open_thread_safe_db`` can re-open it).

    ``state_db.connect`` owns open + migrate (constitution VI), so no
    separate ``open_state_db`` / ``migrate`` calls are needed."""
    db = state_db.connect(tmp_path)
    row = db.execute(
        "SELECT id FROM tenants WHERE slug='install'"
    ).fetchone()
    assert row is not None, "install tenant not seeded by migrate"
    yield db
    db.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run_request(db: sqlite3.Connection,
                 headers: dict, body: bytes | None = None,
                 captured: list[MCPContext] | None = None,
                 sent: list[tuple[int, dict]] | None = None,
                 service_account_email: str = "system") -> None:
    """Drive ``do_POST`` on a hand-rolled handler instance (no socket I/O).

    ``captured`` (optional list) receives the MCPContext passed to the
    dispatch callable.  ``sent`` (optional list) receives
    ``(status, payload)`` tuples from ``_send``.

    The handler is rebuilt inside this helper (the caller's mock
    context, if any, is still active, so the patched authenticator is
    baked in).  ``Content-Length`` is injected so the handler reads the
    body; without it the handler would see an empty body and 400.
    """
    if captured is None:
        captured = []
    if sent is None:
        sent = []

    def capture_dispatch(ctx: MCPContext, tool: str, args: dict):
        captured.append(ctx)
        return {"ok": True, "tool": tool}

    # Build the handler inside the caller's mock.patch context so the
    # patched mcp_authenticator is baked into the handler class.
    handler_cls = mcp_http.build_handler(
        db, dispatch=capture_dispatch,
        service_account_email=service_account_email,
    )
    # object.__new__ bypasses __init__ (no socket).
    inst = object.__new__(handler_cls)
    inst.path = "/mcp"
    hdrs = dict(headers)
    if body is not None:
        hdrs["Content-Length"] = str(len(body))
    inst.headers = hdrs
    import io
    inst.rfile = io.BytesIO(body if body is not None else b"")
    inst._send = lambda status, payload: sent.append((status, payload))
    inst.do_POST()


def _seed_account(db: sqlite3.Connection, email: str, role: str | None) -> None:
    """Create an account row with the given role (role=None lets
    ``next_role_for`` decide: first account is admin, later ones
    reader).  The str-branch tests mock the authenticator, so the
    account row alone is what ``accounts.get_role`` resolves against."""
    _accounts.create_account(db, email, password="pw-" + email, role=role)


# ---------------------------------------------------------------------------
# int-branch: per-tenant service token
# ---------------------------------------------------------------------------

def test_per_tenant_service_token_int_branch(state_db_conn):
    """``(True, <int>)`` → MCPContext with caller_tenant_id=<int>,
    caller_email=None, caller_role='tenant-admin'."""
    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        # mcp_authenticator(db) returns a closure; the closure
        # is what do_POST invokes.  Stub the closure to return the
        # per-tenant success shape.
        auth_mock.return_value = lambda headers: (True, 42, None)
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer per-tenant-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.caller_tenant_id == 42
    assert ctx.caller_email is None
    assert ctx.caller_role == "tenant-admin"
    assert ctx.agent_kind == "http"
    assert ctx.db is not None
    assert sent[-1][0] == 200
    assert sent[-1][1]["ok"] is True


def test_install_tenant_id_1_maps_to_install_admin(state_db_conn):
    """tenant id 1 → caller_role='install-admin'."""
    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (True, 1, None)
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer per-tenant-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.caller_tenant_id == 1
    assert ctx.caller_email is None
    assert ctx.caller_role == "install-admin"
    assert sent[-1][0] == 200


def test_mcp_context_all_fields_int_branch(state_db_conn):
    """All MCPContext fields have the expected types for a per-tenant
    service token."""
    captured: list[MCPContext] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (True, 7, None)
        _run_request(
            state_db_conn,
            headers={},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
        )

    assert len(captured) == 1
    ctx = captured[0]
    assert isinstance(ctx, MCPContext)
    assert ctx.caller_email is None
    assert ctx.caller_role == "tenant-admin"
    assert ctx.caller_tenant_id == 7
    assert ctx.agent_kind == "http"
    assert ctx.config is None


# ---------------------------------------------------------------------------
# str-branch: personal / session / shared service token
# ---------------------------------------------------------------------------

def test_shared_service_token_str_branch(state_db_conn):
    """Shared service token → ``(True, <email>)`` → caller_email set,
    caller_tenant_id=None, role resolved via accounts.get_role."""
    # Seed an account with a known role so get_role resolves.
    _seed_account(state_db_conn, "svc@example.com", "reader")

    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (True, 1, "svc@example.com")
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer shared-service-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.caller_email == "svc@example.com"
    # shared service tokens are scoped to the install tenant (id 1)
    assert ctx.caller_tenant_id == 1
    assert ctx.caller_role == "reader"
    assert ctx.agent_kind == "http"
    assert sent[-1][0] == 200
    assert sent[-1][1]["ok"] is True


def test_personal_token_str_branch(state_db_conn):
    """Personal token → ``(True, <email>)`` → role from accounts table."""
    _seed_account(state_db_conn, "alice@example.com", "admin")

    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (True, 1, "alice@example.com")
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer personal-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.caller_email == "alice@example.com"
    # personal tokens are scoped to the install tenant (id 1)
    assert ctx.caller_tenant_id == 1
    # the seeded account's role is 'admin'
    assert ctx.caller_role == "admin"
    assert sent[-1][0] == 200


def test_ghost_account_is_403_fail_closed(state_db_conn):
    """A known email whose account no longer exists → 403 fail-closed
    (the str-branch account lookup, not a dispatch success)."""
    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (
            False, 1, "account no longer exists",
        )
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer personal-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert captured == []  # dispatch never reached
    assert sent[-1][0] == 403
    assert "account no longer exists" in json.dumps(
        sent[-1][1], ensure_ascii=False,
    )


def test_unknown_credential_is_401(state_db_conn):
    """Unknown credential → 401-class denial; dispatch never reached."""
    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (False, 1, None)
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer bogus"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert captured == []
    assert sent[-1][0] == 401


def test_suspended_tenant_is_403(state_db_conn):
    """A suspended tenant's service token → 403 with the
    'tenant suspended' reason; dispatch never reached."""
    from digital_twins import tenant_service

    tenant_id = tenant_service.provision_tenant(
        state_db_conn, "acme",
    )
    tenant_service.suspend_tenant(state_db_conn, tenant_id)

    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (
            False, 1, "tenant suspended",
        )
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer per-tenant-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert captured == []
    assert sent[-1][0] == 403
    assert "tenant suspended" in json.dumps(
        sent[-1][1], ensure_ascii=False,
    )


def test_deleted_tenant_is_401(state_db_conn):
    """A deleted tenant's service token → 401-class denial (the
    403 'tenant suspended' wording is reserved for suspensions);
    dispatch never reached."""
    from digital_twins import tenant_service

    tenant_id = tenant_service.provision_tenant(state_db_conn, "acme")
    tenant_service.delete_tenant(state_db_conn, tenant_id)

    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []

    with mock.patch.object(
        mcp_http, "mcp_authenticator",
    ) as auth_mock:
        auth_mock.return_value = lambda headers: (False, 1, None)
        _run_request(
            state_db_conn,
            headers={"Authorization": "Bearer per-tenant-token"},
            body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
            captured=captured,
            sent=sent,
        )

    assert captured == []
    assert sent[-1][0] == 401


# ---------------------------------------------------------------------------
# real authenticator (no mocks): end-to-end through mcp_authenticator
# ---------------------------------------------------------------------------

def test_real_authenticator_per_tenant_token(state_db_conn):
    """A per-tenant service token issued via tenant_service
    authenticates end-to-end through the real mcp_authenticator:
    dispatch sees caller_tenant_id=<tenant>, role 'tenant-admin',
    and the request succeeds."""
    from digital_twins.tenant_service import (
        issue_service_token,
        provision_tenant,
    )

    tenant_id = provision_tenant(state_db_conn, "acme")
    token = issue_service_token(state_db_conn, tenant_id)
    assert isinstance(token, str) and len(token) > 0

    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []
    _run_request(
        state_db_conn,
        headers={"Authorization": "Bearer " + token},
        body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
        captured=captured,
        sent=sent,
    )

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.caller_tenant_id == tenant_id
    assert ctx.caller_role == "tenant-admin"
    assert sent[-1][0] == 200
    assert sent[-1][1]["ok"] is True


def test_real_authenticator_shared_service_token(
    state_db_conn, monkeypatch,
):
    """The install-wide shared service token (DT_SERVICE_TOKEN env)
    authenticates end-to-end through the real mcp_authenticator:
    dispatch sees the service account email, no tenant, and the
    account's role.

    ``build_handler`` defaults the service account email to ``"system"``,
    so the shared token resolves to that email; seed the account so
    ``accounts.get_role`` resolves its role (an unseeded email is a
    fail-closed 403)."""
    _accounts.create_account(
        state_db_conn, "system",
        password="pw", role="admin",
    )
    # Shared service token = the env-var credential the real
    # authenticator checks first (hmac.compare_digest).
    monkeypatch.setenv("DT_SERVICE_TOKEN", "install-shared-token")

    captured: list[MCPContext] = []
    sent: list[tuple[int, dict]] = []
    _run_request(
        state_db_conn,
        headers={"Authorization": "Bearer install-shared-token"},
        body=json.dumps({"tool": "kb_health", "args": {}}).encode(),
        captured=captured,
        sent=sent,
    )

    assert len(captured) == 1
    ctx = captured[0]
    # the shared token resolves to the service account email, scoped to
    # the install tenant
    assert ctx.caller_email == "system"
    assert ctx.caller_tenant_id == 1
    assert ctx.caller_role == "admin"
    assert sent[-1][0] == 200
    assert sent[-1][1]["ok"] is True
