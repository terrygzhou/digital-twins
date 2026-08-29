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

import json as _json

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


# ---------------------------------------------------------------------------
# T019: kb_schedule_run
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir):
    """A config dict with a single enabled ``fs`` source over ``fs_dir``.

    Mirrors tests/integration/test_pipeline_owner.py::_cfg so the MCP run
    body exercises the same 001 pipeline the CLI/serve paths do.
    """
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo",
                         "gmail", "fs")}
    sources["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                     "extra": {"dir": str(fs_dir)}}
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": sources,
    }


def _embedder(texts):
    """Stub embedder: fixed 384-dim vectors (the pinned model dim)."""
    return [[0.5] * 384 for _ in texts]


@pytest.fixture()
def fs_dir(tmp_path):
    """A test source dir with one known file (a single chunk)."""
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("alpha note", encoding="utf-8")
    return d


def _run_kwarg_capture(monkeypatch):
    """Patch 001 run_pipeline to record its kwargs and return a RunSummary.

    Returns the capture dict; the fake writes a real (started) audit row so
    the caller can assert on the pipeline's trigger/scheduled_by/owner.
    """
    import json as _json
    from digital_twins.ingest import pipeline as pipe
    from digital_twins.ingest.pipeline import RunSummary
    from digital_twins.state.models import start_audit_run, finish_audit_run

    captured = {}

    def fake_run_pipeline(cfg, db, qdrant, embedder=None, **kw):
        captured["kwargs"] = kw
        captured["cfg"] = cfg
        # Write the audit row exactly as 001 does (the pipeline owns the
        # row; the MCP layer only stamps agent_kind onto it afterwards).
        import uuid as _uuid
        run_id = str(_uuid.uuid4())
        start_audit_run(db, run_id, trigger=kw.get("trigger", "manual"),
                        scheduled_by=kw.get("scheduled_by", "system"))
        counts = {kw.get("source_names", ["?"])[0]: 1}
        finish_audit_run(db, run_id, "ok", counts)
        captured["run_id"] = run_id
        return RunSummary(run_id, counts, 1, "ok")

    monkeypatch.setattr(pipe, "run_pipeline", fake_run_pipeline)
    return captured


def test_kb_schedule_run_calls_pipeline_with_mcp_trigger(db, ctx_factory,
                                                         tmp_path, fs_dir,
                                                         monkeypatch):
    """T019: kb_schedule_run routes through 001 run_pipeline (NOT
    serve_once_tick) with trigger='mcp', scheduled_by=caller, and
    owner=schedule.owner (owner-tagged points, R6), after merging the
    owner's user_config.

    One-record-not-N: the pipeline's deterministic point IDs are the dedup
    authority; owner/agent_kind are payload, never in the point ID. Exactly
    one audit_runs row with trigger='mcp', scheduled_by=<caller>, and
    agent_kind recorded in per_source_counts (R13/D6).
    """
    from qdrant_client import QdrantClient
    from digital_twins.mcp import dispatch as disp_mod

    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    cfg = _cfg(tmp_path, fs_dir)
    in_memory = QdrantClient(":memory:")

    captured = _run_kwarg_capture(monkeypatch)
    # Wire the resolved config + qdrant/embedder into the MCP run body.
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda cfg: (lambda: in_memory))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda cfg: _embedder)

    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test-agent",
    })
    assert result.get("ok") is True, (
        f"run: expected success, got {result}"
    )

    # The pipeline was called with the D4 contract kwargs.
    kw = captured["kwargs"]
    assert kw.get("trigger") == "mcp", (
        f"pipeline parity: trigger must be 'mcp', got {kw.get('trigger')}"
    )
    assert kw.get("scheduled_by") == "alice@example.com", (
        f"pipeline parity: scheduled_by must be the caller, "
        f"got {kw.get('scheduled_by')}"
    )
    assert kw.get("owner") == "alice@example.com", (
        f"pipeline parity (R6): owner must be the schedule owner, "
        f"got {kw.get('owner')}"
    )
    assert kw.get("source_names") == ["fs"], (
        f"pipeline parity: source_names must be [schedule.source], "
        f"got {kw.get('source_names')}"
    )

    # Exactly one audit row: trigger='mcp', scheduled_by=<caller>,
    # agent_kind recorded in per_source_counts (R13/D6).
    rows = db.execute(
        "SELECT trigger, scheduled_by, status, per_source_counts "
        "FROM audit_runs"
    ).fetchall()
    assert len(rows) == 1, (
        f"one audit row per run: expected 1, got {len(rows)}: {rows}"
    )
    trigger, scheduled_by, status, counts_json = rows[0]
    assert trigger == "mcp"
    assert scheduled_by == "alice@example.com"
    assert status == "ok"
    counts = _json.loads(counts_json)
    assert counts.get("agent_kind") == "test-agent", (
        f"R13/D6: agent_kind must be recorded in per_source_counts, "
        f"got {counts}"
    )
    assert counts.get("fs") == 1, (
        f"per_source_counts must carry the real source counts, got {counts}"
    )

    # The returned data exposes the R2 run-record fields.
    data = result.get("data", {})
    assert data.get("run_id") == captured["run_id"]
    assert data.get("status") == "ok"
    assert data.get("agent_kind") == "test-agent"


def test_kb_schedule_run_disabled_source(db, ctx_factory, tmp_path, fs_dir,
                                         monkeypatch):
    """T019: when the schedule's source is disabled, kb_schedule_run returns
    source_disabled (nothing to run) and writes no audit row."""
    from digital_twins.mcp import dispatch as disp_mod

    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    cfg = _cfg(tmp_path, fs_dir)
    cfg["sources"]["fs"]["enabled"] = False  # disable the source

    captured = _run_kwarg_capture(monkeypatch)
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db: cfg)

    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "source_disabled", (
        f"disabled source: expected source_disabled, got {result}"
    )
    # The pipeline was never called and no audit row was written.
    assert "kwargs" not in captured, (
        "disabled source: run_pipeline must not be called"
    )
    assert _audit_runs_count(db) == 0, (
        "disabled source: no audit row should be written"
    )


def test_kb_schedule_run_failed_pipeline(db, ctx_factory, tmp_path, fs_dir,
                                         monkeypatch):
    """T019: when run_pipeline returns a 'failed' outcome, kb_schedule_run
    returns run_failed and the row is audited as 'failed'."""
    from qdrant_client import QdrantClient
    from digital_twins.ingest import pipeline as pipe
    from digital_twins.ingest.pipeline import RunSummary
    from digital_twins.mcp import dispatch as disp_mod

    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    cfg = _cfg(tmp_path, fs_dir)
    in_memory = QdrantClient(":memory:")

    def fake_failed(cfg, db, qdrant, embedder=None, **kw):
        import uuid as _uuid
        from digital_twins.state.models import start_audit_run, finish_audit_run
        run_id = str(_uuid.uuid4())
        start_audit_run(db, run_id, trigger=kw.get("trigger", "manual"),
                        scheduled_by=kw.get("scheduled_by", "system"))
        finish_audit_run(db, run_id, "failed", {"fs": 0})
        return RunSummary(run_id, {"fs": 0}, 0, "failed")

    monkeypatch.setattr(pipe, "run_pipeline", fake_failed)
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda cfg: (lambda: in_memory))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda cfg: _embedder)

    ctx = ctx_factory("alice@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "run_failed", (
        f"failed pipeline: expected run_failed, got {result}"
    )
    rows = db.execute(
        "SELECT status, trigger FROM audit_runs"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "failed" and rows[0][1] == "mcp", (
        f"failed run: expected one 'failed'/'mcp' audit row, got {rows}"
    )


def test_kb_schedule_run_requires_trigger_run(db, ctx_factory, tmp_path,
                                              fs_dir):
    """T019: a reader (no trigger_run) → permission_denied."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    ctx = ctx_factory("reader@example.com", "reader")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"reader run: expected permission_denied, got {result}"
    )
    assert _audit_runs_count(db) == 0, (
        "reader run: denied before any pipeline work; no audit row"
    )


def test_kb_schedule_run_owner_only(db, ctx_factory):
    """T019: a non-owner, non-admin caller gets schedule_not_found (R5)."""
    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    ctx = ctx_factory("bob@example.com", "scheduler")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "test",
    })
    assert _error_code(result) == "schedule_not_found", (
        f"non-owner run: expected schedule_not_found, got {result}"
    )
    assert _audit_runs_count(db) == 0


def test_kb_schedule_run_admin_can_run(db, ctx_factory, tmp_path, fs_dir,
                                       monkeypatch):
    """T019: an admin can run another user's schedule (R9). scheduled_by is
    still the *caller* (the admin), owner is the schedule owner."""
    from qdrant_client import QdrantClient
    from digital_twins.mcp import dispatch as disp_mod

    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    cfg = _cfg(tmp_path, fs_dir)
    in_memory = QdrantClient(":memory:")
    captured = _run_kwarg_capture(monkeypatch)
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda cfg: (lambda: in_memory))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda cfg: _embedder)

    ctx = ctx_factory("admin@example.com", "admin")
    result = _dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
        "agent_kind": "admin-agent",
    })
    assert result.get("ok") is True, (
        f"admin run: expected success, got {result}"
    )
    kw = captured["kwargs"]
    assert kw.get("scheduled_by") == "admin@example.com"
    assert kw.get("owner") == "alice@example.com"


# ---------------------------------------------------------------------------
# T021: kb_run_history
# ---------------------------------------------------------------------------

def _seed_run(db, scheduled_by, source="fs", started="2025-07-01T00:00:00",
              status="ok"):
    """Seed one audit_runs row and return its run_id."""
    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, "
        "trigger, scheduled_by, per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, started, started, status, "schedule", scheduled_by,
         _json.dumps({source: 1})),
    )
    db.commit()
    return run_id


def test_kb_run_history_own_scope(db, ctx_factory):
    """T021: own scope (user omitted) returns the caller's runs, R2 field
    names, and writes NO audit row. R13: mcp_history_query rows are excluded."""
    own = _seed_run(db, "alice@example.com", source="fs")
    _seed_run(db, "bob@example.com", source="fs")  # another user's run

    ctx = ctx_factory("alice@example.com", "reader")
    result = _dispatch(ctx, "kb_run_history", {"agent_kind": "test"})
    assert result.get("ok") is True, (
        f"own-scope history: expected success, got {result}"
    )
    runs = result.get("data", {}).get("runs", [])
    assert len(runs) == 1, (
        f"own-scope history: expected only alice's run, got {len(runs)}: {runs}"
    )
    assert runs[0]["run_id"] == own
    # R2 field names on every record.
    r2_fields = ("run_id", "started_at", "completed_at", "status",
                 "trigger", "scheduled_by", "per_source_counts")
    for field in r2_fields:
        assert field in runs[0], (
            f"R2 field names: missing {field!r}, got {list(runs[0].keys())}"
        )
    assert result.get("data", {}).get("count") == 1
    # No audit row is written for an own-scope read (R11-ish; not a run).
    assert _audit_runs_count(db) == 2, (
        f"own-scope history: no new audit row expected, "
        f"got {_audit_runs_count(db)} rows (seeded 2)"
    )


def test_kb_run_history_r13_excludes_access_log_rows(db, ctx_factory):
    """T019/T021 (R13): an mcp_history_query access-log row is never
    mistaken for a run — it is excluded from the read result."""
    own = _seed_run(db, "alice@example.com", source="fs")
    # Seed an access-log row (the R8 shape) attributed to alice.
    import uuid as _uuid
    log_run_id = str(_uuid.uuid4())
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, "
        "trigger, scheduled_by, per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (log_run_id, "2025-07-02T00:00:00", "2025-07-02T00:00:00", "ok",
         "mcp", "alice@example.com",
         _json.dumps({"mcp_history_query": {"target_user": "bob@example.com",
                                            "agent_kind": "test"}})),
    )
    db.commit()

    ctx = ctx_factory("alice@example.com", "reader")
    result = _dispatch(ctx, "kb_run_history", {"agent_kind": "test"})
    assert result.get("ok") is True
    runs = result.get("data", {}).get("runs", [])
    returned_ids = {r["run_id"] for r in runs}
    assert own in returned_ids, "own run must be present"
    assert log_run_id not in returned_ids, (
        "R13: an mcp_history_query row must be excluded from the result"
    )
    assert result.get("data", {}).get("count") == 1


def test_kb_run_history_admin_cross_user_writes_access_log(db, ctx_factory):
    """T021: an admin cross-user read (user != caller, role admin) →
    list_runs(all_users=True) AND writes one R8 access-log row:
    run_id=fresh uuid4, status='ok', trigger='mcp', scheduled_by=<admin>,
    per_source_counts={"mcp_history_query": {target_user, agent_kind}}."""
    target_run = _seed_run(db, "bob@example.com", source="fs")

    ctx = ctx_factory("admin@example.com", "admin", agent_kind="admin-agent")
    result = _dispatch(ctx, "kb_run_history", {
        "user": "bob@example.com",
    })
    assert result.get("ok") is True, (
        f"admin cross-user history: expected success, got {result}"
    )
    runs = result.get("data", {}).get("runs", [])
    assert len(runs) == 1 and runs[0]["run_id"] == target_run, (
        f"admin cross-user history: expected bob's run, got {runs}"
    )

    # The access-log row was written (R8).
    log_rows = db.execute(
        "SELECT run_id, status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs WHERE trigger='mcp' AND scheduled_by='admin@example.com'"
    ).fetchall()
    assert len(log_rows) == 1, (
        f"R8: expected exactly one access-log row, got {len(log_rows)}: "
        f"{log_rows}"
    )
    run_id, status, trigger, scheduled_by, counts_json = log_rows[0]
    assert status == "ok"
    assert trigger == "mcp"
    assert scheduled_by == "admin@example.com"
    counts = _json.loads(counts_json)
    mq = counts.get("mcp_history_query")
    assert mq is not None, (
        f"R8: per_source_counts must carry the mcp_history_query block, "
        f"got {counts}"
    )
    assert mq.get("target_user") == "bob@example.com"
    assert mq.get("agent_kind") == "admin-agent"
    # The access-log run_id is a fresh uuid (distinct from the target run).
    assert run_id != target_run


def test_kb_run_history_cross_user_requires_admin(db, ctx_factory):
    """T021: a non-admin cross-user request → permission_denied."""
    _seed_run(db, "bob@example.com", source="fs")
    ctx = ctx_factory("alice@example.com", "scheduler")  # not admin
    result = _dispatch(ctx, "kb_run_history", {
        "user": "bob@example.com",
        "agent_kind": "test",
    })
    assert _error_code(result) == "permission_denied", (
        f"non-admin cross-user history: expected permission_denied, got {result}"
    )
    # No access-log row was written.
    assert _audit_runs_count(db) == 1, (
        f"non-admin cross-user: no access-log row expected, "
        f"got {_audit_runs_count(db)} rows (seeded 1)"
    )


def test_kb_run_history_source_filter(db, ctx_factory):
    """T021: the optional `source` filter narrows the result by the run's
    per_source_counts key."""
    _seed_run(db, "alice@example.com", source="fs")
    _seed_run(db, "alice@example.com", source="hermes")
    ctx = ctx_factory("alice@example.com", "reader")
    result = _dispatch(ctx, "kb_run_history", {
        "source": "hermes",
        "agent_kind": "test",
    })
    assert result.get("ok") is True
    runs = result.get("data", {}).get("runs", [])
    assert len(runs) == 1, (
        f"source filter: expected only the hermes run, got {len(runs)}: {runs}"
    )
    counts = runs[0]["per_source_counts"]
    counts = _json.loads(counts) if isinstance(counts, str) else counts
    assert "hermes" in counts
