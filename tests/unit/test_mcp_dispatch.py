"""T009 (RED): dispatch — the MCP tool executor (feature 004, R4/R5/R9).

Asserts ``dispatch(ctx, tool_name, args) -> dict`` enforces, in order:

1. **Role-gate** (via ``accounts.require_capability``): a caller whose role
   lacks the tool's capability → ``permission_denied``.
2. **Owner-scope** (via ``can_access_schedule``): a caller who cannot see
   the target schedule → ``schedule_not_found``.
3. **Tool body**: when both gates pass, the body runs and its result is
   returned.

The 6 scenarios (T009 a–f):

(a) reader calling create → ``permission_denied`` (reader lacks
    ``schedule_crud``).
(b) admin calling list with ``all_users=True`` → success.
(c) scheduler requesting ``all_users=True`` on list → ``permission_denied``
    (scheduler lacks ``view_all_history``).
(d) owner calling run on own schedule → success.
(e) non-owner, non-admin calling run on other's schedule →
    ``schedule_not_found``.
(f) role-gate denial happens **before** owner-scope (a reader calling run
    on someone else's schedule gets ``permission_denied``, not
    ``schedule_not_found``).

For T009/T010 the tool bodies are stubs: a ``tool_bodies`` dict mapping tool
name → callable. The real bodies land in Phase 3/4.
"""
from __future__ import annotations

import pytest

from digital_twins.accounts import create_account
from digital_twins.scheduler.schedules import create_schedule
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) state DB with the schedules table."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def ctx_factory(db):
    """Build an MCPContext with the given email + role + a real db."""
    from digital_twins.mcp.registry import MCPContext

    def _make(email: str, role: str, agent_kind: str = "test-agent"):
        # Ensure the account exists so get_role can resolve it (the
        # dispatch role-gate uses ctx.caller_role directly, but the
        # list_runs all_users path may call get_role).
        try:
            create_account(db, email, f"pw-{email}")
        except Exception:
            pass
        # Force the role in case create_account assigned a different one
        # (first row → admin).
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


def _make_schedule(db, owner: str, source: str = "hermes") -> dict:
    """Create a schedule row and return it."""
    return create_schedule(db, owner=owner, source=source, preset="daily")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dispatch(ctx, tool_name: str, args: dict) -> dict:
    from digital_twins.mcp.dispatch import dispatch
    return dispatch(ctx, tool_name, args)


def _error_code(result: dict) -> str | None:
    """Extract the error code from a dispatch result, or None on success."""
    if result.get("ok"):
        return None
    return result.get("error", {}).get("code")


# ---------------------------------------------------------------------------
# (a) reader calling create → permission_denied
# ---------------------------------------------------------------------------

def test_reader_create_permission_denied(db, ctx_factory):
    """A reader lacks schedule_crud → create is denied with permission_denied."""
    ctx = ctx_factory("reader@example.com", "reader")
    result = _dispatch(ctx, "kb_schedule_create", {
        "source": "hermes",
        "preset": "daily",
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"reader calling create: expected permission_denied, got {result}"
    )


# ---------------------------------------------------------------------------
# (b) admin calling list(all_users=True) → success
# ---------------------------------------------------------------------------

def test_admin_list_all_users_success(db, ctx_factory):
    """An admin with all_users=True lists all schedules → ok."""
    # Seed two owners so the admin sees both.
    _make_schedule(db, "owner_a@example.com")
    _make_schedule(db, "owner_b@example.com")
    ctx = ctx_factory("admin@example.com", "admin")
    result = _dispatch(ctx, "kb_schedule_list", {
        "all_users": True,
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"admin list(all_users=True): expected success, got {result}"
    )
    schedules = result.get("data", {}).get("schedules", [])
    assert len(schedules) == 2, (
        f"admin list(all_users=True): expected 2 schedules, got {len(schedules)}"
    )


# ---------------------------------------------------------------------------
# (c) scheduler requesting all_users=True → permission_denied
# ---------------------------------------------------------------------------

def test_scheduler_list_all_users_permission_denied(db, ctx_factory):
    """A scheduler lacks view_all_history → all_users=True is denied."""
    _make_schedule(db, "owner_a@example.com")
    ctx = ctx_factory("scheduler@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_list", {
        "all_users": True,
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"scheduler list(all_users=True): expected permission_denied, got {result}"
    )


# ---------------------------------------------------------------------------
# (d) owner calling run on own schedule → success
# ---------------------------------------------------------------------------

def test_owner_run_own_schedule_success(db, ctx_factory, monkeypatch):
    """An owner running their own schedule passes both gates and reaches the
    tool body (T009 d).

    T009 was written when the body was a stub (ok). Now that T020 implements
    the real body, the body resolves the config and — with the hermes source
    disabled in the default config — returns ``source_disabled`` (nothing to
    run).  The point of this test is that the role-gate + owner-scope both
    pass and the *body* runs: a ``source_disabled`` (the body's answer) proves
    the body executed, not a gate's ``permission_denied``/``schedule_not_found``.
    """
    sched = _make_schedule(db, "owner@example.com")
    ctx = ctx_factory("owner@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    code = _error_code(result)
    # source_disabled (body ran, source disabled) OR ok (source enabled in the
    # resolved config) both prove the gates passed and the body executed.
    assert code in ("source_disabled", None), (
        f"owner run own schedule: expected the body to run "
        f"(source_disabled or success), got {code}: {result}"
    )


# ---------------------------------------------------------------------------
# (e) non-owner, non-admin calling run on other's schedule → schedule_not_found
# ---------------------------------------------------------------------------

def test_non_owner_run_other_schedule_not_found(db, ctx_factory):
    """A non-owner, non-admin running someone else's schedule →
    schedule_not_found (the cross-user isolation guarantee, R5)."""
    sched = _make_schedule(db, "other@example.com")
    ctx = ctx_factory("intruder@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "schedule_not_found", (
        f"non-owner run on other's schedule: expected schedule_not_found, "
        f"got {result}"
    )


# ---------------------------------------------------------------------------
# (f) role-gate denial happens BEFORE owner-scope
# ---------------------------------------------------------------------------

def test_role_gate_before_owner_scope(db, ctx_factory):
    """A reader (no trigger_run) calling run on someone else's schedule
    gets permission_denied (role-gate), NOT schedule_not_found
    (owner-scope). The role-gate must fire first."""
    sched = _make_schedule(db, "other@example.com")
    ctx = ctx_factory("reader@example.com", "reader")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"reader run on other's schedule: expected permission_denied "
        f"(role-gate before owner-scope), got {result}"
    )
