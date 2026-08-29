"""T023 (RED): BR-10 stub tools — not_implemented_yet (feature 004, R10).

The four BR-10 stubs (``kb_search``, ``kb_chat``, ``kb_ingest``, ``kb_health``)
are registered in ``tools/list`` so a fresh client sees a complete, stable
registry, but each body immediately returns ``code=not_implemented_yet`` (the
501-ish error envelope). They do nothing: no role-gate, no owner-scope, no
audit row (contracts/scheduler.md §"The four BR-10 stubs").

This test asserts, via the real ``dispatch`` path, that each of the four stubs
returns the stable ``not_implemented_yet`` error code with a message that names
the follow-up slice.
"""
from __future__ import annotations

import pytest

from digital_twins.accounts import create_account
from digital_twins.mcp.dispatch import dispatch
from digital_twins.mcp.registry import MCPContext
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


STUB_TOOLS = ["kb_search", "kb_chat", "kb_ingest", "kb_health"]


@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) state DB."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def ctx_factory(db):
    """Build an MCPContext with the given email + role + a real db."""

    def _make(email: str, role: str, agent_kind: str = "test-agent"):
        try:
            create_account(db, email, f"pw-{email}")
        except Exception:
            pass
        db.execute(
            "UPDATE accounts SET role=? WHERE email=?", (role, email)
        )
        db.commit()
        return MCPContext(
            db=db,
            caller_email=email,
            caller_role=role,
            agent_kind=agent_kind,
        )

    return _make


@pytest.mark.parametrize("tool_name", STUB_TOOLS)
def test_stub_returns_not_implemented_yet(db, ctx_factory, tool_name):
    """Each BR-10 stub, called via dispatch, returns code=not_implemented_yet."""
    ctx = ctx_factory("reader@example.com", "reader")
    result = dispatch(ctx, tool_name, {"agent_kind": "test"})
    assert result.get("ok") is False, (
        f"{tool_name}: expected a non-ok envelope, got {result}"
    )
    error = result.get("error") or {}
    assert error.get("code") == "not_implemented_yet", (
        f"{tool_name}: expected code=not_implemented_yet, got {result}"
    )
    # R10: the message names the follow-up slice (the tool + that it is a
    # BR-10 follow-up not implemented in 004).
    message = error.get("message") or ""
    assert tool_name in message, (
        f"{tool_name}: expected the message to name the follow-up slice, "
        f"got {message!r}"
    )
    assert "BR-10" in message, (
        f"{tool_name}: expected the message to mention the BR-10 follow-up, "
        f"got {message!r}"
    )
