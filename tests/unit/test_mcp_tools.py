"""T011 + T013 (RED): kb_schedule_list and kb_schedule_create tool bodies.

Phase 3 of feature 004-mcp-scheduler-tools: the first two of the six real
tool bodies.  These tests assert the R2 contract field names and R11
(no audit_runs row for CRUD) that the Phase-2 stubs and the existing
dispatch tests do not cover.

T011 — kb_schedule_list:
    - Own scope (all_users=false) returns only the caller's schedules.
    - Admin with all_users=True gets all users' schedules.
    - Returned objects carry R2 field names (schedule_id, not id).
    - No audit_runs row is written (a read, not a run).

T013 — kb_schedule_create:
    - Creates a schedule row (persisted in the schedules table).
    - Requires schedule_crud (reader → permission_denied).
    - Records owner = caller_email.
    - R11: no audit_runs row (CRUD is not a run).
    - R2: returned object uses schedule_id, not id.
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


def _dispatch(ctx, tool_name: str, args: dict) -> dict:
    from digital_twins.mcp.dispatch import dispatch
    return dispatch(ctx, tool_name, args)


def _error_code(result: dict) -> str | None:
    """Extract the error code from a dispatch result, or None on success."""
    if result.get("ok"):
        return None
    return result.get("error", {}).get("code")


def _audit_runs_count(db) -> int:
    """Count rows in the audit_runs table."""
    return db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]


# ---------------------------------------------------------------------------
# T011: kb_schedule_list
# ---------------------------------------------------------------------------

def test_kb_schedule_list_own_scope(db, ctx_factory):
    """Own scope (all_users=false) returns only the caller's schedules.

    Seeds two schedules for different owners; the caller sees only their own.
    """
    # Seed: caller has one schedule, another user has one.
    _sched_a = create_schedule(db, owner="alice@example.com",
                               source="hermes", preset="daily")
    _sched_b = create_schedule(db, owner="bob@example.com",
                               source="fs", preset="hourly")

    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_list", {
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"own-scope list: expected success, got {result}"
    )
    schedules = result.get("data", {}).get("schedules", [])
    # Alice sees only her own schedule (bob's is excluded by owner scope).
    assert len(schedules) == 1, (
        f"own-scope list: expected 1 schedule, got {len(schedules)}: "
        f"{schedules}"
    )
    assert schedules[0]["owner"] == "alice@example.com"


def test_kb_schedule_list_admin_all_users(db, ctx_factory):
    """Admin with all_users=True gets all users' schedules."""
    create_schedule(db, owner="alice@example.com",
                    source="hermes", preset="daily")
    create_schedule(db, owner="bob@example.com",
                    source="fs", preset="hourly")

    ctx = ctx_factory("admin@example.com", "admin")
    result = _dispatch(ctx, "kb_schedule_list", {
        "all_users": True,
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"admin all_users list: expected success, got {result}"
    )
    schedules = result.get("data", {}).get("schedules", [])
    assert len(schedules) == 2, (
        f"admin all_users list: expected 2 schedules, got {len(schedules)}"
    )
    owners = {s["owner"] for s in schedules}
    assert owners == {"alice@example.com", "bob@example.com"}


def test_kb_schedule_list_r2_fields(db, ctx_factory):
    """Returned schedule objects carry R2 field names.

    The contract (004 contracts/scheduler.md) specifies ``schedule_id``
    as the field name (mirroring 003 contracts/scheduler.md).  The raw DB
    column is ``id``; the MCP layer must expose it as ``schedule_id``.
    """
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_list", {
        "agent_kind": "test",
    })
    assert result.get("ok") is True
    schedules = result.get("data", {}).get("schedules", [])
    assert len(schedules) == 1
    row = schedules[0]
    # R2: the schedule id field is named "schedule_id", not "id".
    assert "schedule_id" in row, (
        f"R2 field names: expected 'schedule_id' in returned object, "
        f"got keys: {list(row.keys())}"
    )
    assert row["schedule_id"] == sched["id"], (
        f"R2 schedule_id: expected {sched['id']}, got {row['schedule_id']}"
    )
    # All R2 fields must be present.
    r2_fields = ("schedule_id", "owner", "source", "preset", "param",
                 "fire_time", "enabled", "next_fire_at", "acl",
                 "created_at", "updated_at")
    for field in r2_fields:
        assert field in row, (
            f"R2 field names: missing {field!r} in returned object, "
            f"got keys: {list(row.keys())}"
        )


def test_kb_schedule_list_no_audit_runs(db, ctx_factory):
    """A list call writes no audit_runs row (a read, not a run)."""
    create_schedule(db, owner="alice@example.com",
                    source="hermes", preset="daily")
    ctx = ctx_factory("alice@example.com", "scheduler")
    _dispatch(ctx, "kb_schedule_list", {"agent_kind": "test"})
    assert _audit_runs_count(db) == 0, (
        "R11: list_schedules must not write an audit_runs row"
    )


# ---------------------------------------------------------------------------
# T013: kb_schedule_create
# ---------------------------------------------------------------------------

def test_kb_schedule_create_creates_schedule(db, ctx_factory):
    """kb_schedule_create persists a schedule row in the schedules table."""
    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_create", {
        "source": "hermes",
        "preset": "daily",
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"create: expected success, got {result}"
    )
    # Verify a schedule row was persisted.
    rows = db.execute(
        "SELECT * FROM schedules WHERE owner = ?",
        ("alice@example.com",),
    ).fetchall()
    assert len(rows) == 1, (
        f"create: expected 1 schedule row in DB, got {len(rows)}"
    )
    # The returned data must contain the new schedule.
    data = result.get("data", {})
    schedule = data.get("schedule")
    assert schedule is not None, (
        f"create: expected data.schedule, got data keys: {list(data.keys())}"
    )


def test_kb_schedule_create_requires_schedule_crud(db, ctx_factory):
    """A reader (no schedule_crud) → permission_denied."""
    ctx = ctx_factory("reader@example.com", "reader")
    result = _dispatch(ctx, "kb_schedule_create", {
        "source": "hermes",
        "preset": "daily",
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"reader create: expected permission_denied, got {result}"
    )
    # No schedule row should have been created.
    rows = db.execute(
        "SELECT * FROM schedules WHERE owner = ?",
        ("reader@example.com",),
    ).fetchall()
    assert len(rows) == 0, (
        f"reader create: expected no schedule row, got {len(rows)}"
    )


def test_kb_schedule_create_records_owner(db, ctx_factory):
    """The created schedule's owner is the caller's email."""
    ctx = ctx_factory("bob@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_create", {
        "source": "fs",
        "preset": "weekly",
        "agent_kind": "test",
    })
    assert result.get("ok") is True
    schedule = result.get("data", {}).get("schedule", {})
    assert schedule.get("owner") == "bob@example.com", (
        f"create owner: expected 'bob@example.com', got {schedule.get('owner')}"
    )


def test_kb_schedule_create_no_audit_runs(db, ctx_factory):
    """R11: a create (CRUD) writes no audit_runs row."""
    ctx = ctx_factory("alice@example.com", "scheduler")
    _dispatch(ctx, "kb_schedule_create", {
        "source": "hermes",
        "preset": "daily",
        "agent_kind": "test",
    })
    assert _audit_runs_count(db) == 0, (
        "R11: create_schedule must not write an audit_runs row "
        "(CRUD is not a run)"
    )


def test_kb_schedule_create_r2_fields(db, ctx_factory):
    """R2: the returned schedule object uses schedule_id, not id."""
    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_create", {
        "source": "hermes",
        "preset": "daily",
        "agent_kind": "test",
    })
    assert result.get("ok") is True
    schedule = result.get("data", {}).get("schedule", {})
    assert "schedule_id" in schedule, (
        f"R2 field names: expected 'schedule_id' in returned schedule, "
        f"got keys: {list(schedule.keys())}"
    )
    # All R2 fields must be present.
    r2_fields = ("schedule_id", "owner", "source", "preset", "param",
                 "fire_time", "enabled", "next_fire_at", "acl",
                 "created_at", "updated_at")
    for field in r2_fields:
        assert field in schedule, (
            f"R2 field names: missing {field!r} in returned schedule, "
            f"got keys: {list(schedule.keys())}"
        )


# ---------------------------------------------------------------------------
# T015: kb_schedule_update
# ---------------------------------------------------------------------------

def test_kb_schedule_update_updates_schedule(db, ctx_factory):
    """kb_schedule_update mutates a schedule and returns the updated row."""
    # Seed a schedule for alice.
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    sched_id = sched["id"]

    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_update", {
        "schedule_id": sched_id,
        "enabled": False,
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"update: expected success, got {result}"
    )
    # The returned data must contain the updated schedule.
    data = result.get("data", {})
    schedule = data.get("schedule")
    assert schedule is not None, (
        f"update: expected data.schedule, got data keys: {list(data.keys())}"
    )
    assert schedule["enabled"] == 0, (
        f"update: expected enabled=0 (False), got {schedule['enabled']}"
    )
    # Verify the change persisted in the DB.
    row = db.execute(
        "SELECT enabled FROM schedules WHERE id = ?", (sched_id,)
    ).fetchone()
    assert row is not None, "update: schedule row should still exist in DB"
    assert row[0] == 0, (
        f"update: DB row expected enabled=0, got {row[0]}"
    )


def test_kb_schedule_update_requires_schedule_crud(db, ctx_factory):
    """A reader (no schedule_crud) → permission_denied."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("reader@example.com", "reader")
    result = _dispatch(ctx, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "enabled": False,
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"reader update: expected permission_denied, got {result}"
    )


def test_kb_schedule_update_owner_only(db, ctx_factory):
    """R5/R9: a non-owner, non-admin caller gets schedule_not_found.

    Alice owns the schedule; Bob (a scheduler, not admin) tries to update it.
    The response must be schedule_not_found (no leak of the schedule's
    existence).
    """
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("bob@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "enabled": False,
        "agent_kind": "test",
    })
    assert _error_code(result) == "schedule_not_found", (
        f"non-owner update: expected schedule_not_found, got {result}"
    )
    # Verify the schedule was NOT modified.
    row = db.execute(
        "SELECT enabled FROM schedules WHERE id = ?", (sched["id"],)
    ).fetchone()
    assert row is not None and row[0] == 1, (
        f"non-owner update: schedule should be unchanged (enabled=1), "
        f"got {row}"
    )


def test_kb_schedule_update_admin_can_update(db, ctx_factory):
    """R9: an admin caller can update another user's schedule."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("admin@example.com", "admin")
    result = _dispatch(ctx, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "enabled": False,
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"admin update: expected success, got {result}"
    )
    # Verify the change persisted.
    row = db.execute(
        "SELECT enabled FROM schedules WHERE id = ?", (sched["id"],)
    ).fetchone()
    assert row is not None and row[0] == 0, (
        f"admin update: DB row expected enabled=0, got {row}"
    )


def test_kb_schedule_update_no_audit_runs(db, ctx_factory):
    """R11: an update (CRUD) writes no audit_runs row."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("alice@example.com", "scheduler")
    _dispatch(ctx, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "enabled": False,
        "agent_kind": "test",
    })
    assert _audit_runs_count(db) == 0, (
        "R11: update_schedule must not write an audit_runs row "
        "(CRUD is not a run)"
    )


def test_kb_schedule_update_r2_fields(db, ctx_factory):
    """R2: the returned schedule object uses schedule_id, not id."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "enabled": False,
        "agent_kind": "test",
    })
    assert result.get("ok") is True
    schedule = result.get("data", {}).get("schedule", {})
    assert "schedule_id" in schedule, (
        f"R2 field names: expected 'schedule_id' in returned schedule, "
        f"got keys: {list(schedule.keys())}"
    )
    assert schedule["schedule_id"] == sched["id"], (
        f"R2 schedule_id: expected {sched['id']}, got {schedule['schedule_id']}"
    )


# ---------------------------------------------------------------------------
# T017: kb_schedule_delete
# ---------------------------------------------------------------------------

def test_kb_schedule_delete_deletes_schedule(db, ctx_factory):
    """kb_schedule_delete removes a schedule row and returns the deleted id."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    sched_id = sched["id"]

    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_delete", {
        "schedule_id": sched_id,
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"delete: expected success, got {result}"
    )
    # The returned data must contain the deleted schedule_id.
    data = result.get("data", {})
    assert data.get("deleted") == sched_id, (
        f"delete: expected data.deleted={sched_id}, got {data}"
    )
    # Verify the row is gone from the DB.
    row = db.execute(
        "SELECT COUNT(*) FROM schedules WHERE id = ?", (sched_id,)
    ).fetchone()
    assert row[0] == 0, (
        f"delete: schedule row should be removed, got {row[0]} rows"
    )


def test_kb_schedule_delete_requires_schedule_crud(db, ctx_factory):
    """A reader (no schedule_crud) → permission_denied."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("reader@example.com", "reader")
    result = _dispatch(ctx, "kb_schedule_delete", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"reader delete: expected permission_denied, got {result}"
    )


def test_kb_schedule_delete_owner_only(db, ctx_factory):
    """R5/R9: a non-owner, non-admin caller gets schedule_not_found.

    Alice owns the schedule; Bob (a scheduler, not admin) tries to delete it.
    The response must be schedule_not_found (no leak of the schedule's
    existence).
    """
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("bob@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_delete", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "schedule_not_found", (
        f"non-owner delete: expected schedule_not_found, got {result}"
    )
    # Verify the schedule was NOT deleted.
    row = db.execute(
        "SELECT COUNT(*) FROM schedules WHERE id = ?", (sched["id"],)
    ).fetchone()
    assert row[0] == 1, (
        f"non-owner delete: schedule should still exist, got {row[0]} rows"
    )


def test_kb_schedule_delete_admin_can_delete(db, ctx_factory):
    """R9: an admin caller can delete another user's schedule."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("admin@example.com", "admin")
    result = _dispatch(ctx, "kb_schedule_delete", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert result.get("ok") is True, (
        f"admin delete: expected success, got {result}"
    )
    # Verify the row is gone.
    row = db.execute(
        "SELECT COUNT(*) FROM schedules WHERE id = ?", (sched["id"],)
    ).fetchone()
    assert row[0] == 0, (
        f"admin delete: schedule row should be removed, got {row[0]} rows"
    )


def test_kb_schedule_delete_no_audit_runs(db, ctx_factory):
    """R11: a delete (CRUD) writes no audit_runs row."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="hermes", preset="daily")
    ctx = ctx_factory("alice@example.com", "scheduler")
    _dispatch(ctx, "kb_schedule_delete", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _audit_runs_count(db) == 0, (
        "R11: delete_schedule must not write an audit_runs row "
        "(CRUD is not a run)"
    )
