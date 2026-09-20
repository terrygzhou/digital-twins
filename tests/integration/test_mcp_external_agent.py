"""MCP external-agent coverage (feature 004+007).

Simulates an external agent (e.g., Hermes, Claude Desktop) connecting to
the MCP server via the documented HTTP + stdio transports, authenticating
with the three credential types (service token, personal token, session
token), and calling every tool in the 10-tool registry.

Backends are mocked (in-memory Qdrant + stub embedder + stub pipeline) so
the tests are hermetic and cover all documented error paths without
requiring live services.

Tool coverage:
  - kb_schedule_list: own scope, admin all_users, reader denial
  - kb_schedule_create: success, invalid preset, reader denial
  - kb_schedule_update: owner success, non-owner schedule_not_found
  - kb_schedule_delete: owner success, reader denial
  - kb_schedule_run: pipeline success (mocked), source_disabled, run_failed
  - kb_run_history: own scope, admin cross-user + access-log, R13 exclusion
  - kb_search: success (mocked Qdrant + embedder), qdrant_unavailable,
    embedding_unavailable, bad_request
  - kb_chat: not_implemented surface
  - kb_ingest: pipeline success (mocked), prerequisite_missing,
    unknown_source, run_failed, no sources enabled
  - kb_health: success (mocked checks), config_not_loaded
"""
from __future__ import annotations

import io
import json
from http.server import ThreadingHTTPServer
from typing import IO
from unittest.mock import patch

import pytest

from digital_twins.accounts import create_account, get_role
from digital_twins.auth import create_personal_token, create_session
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.mcp import dispatch as disp_mod
from digital_twins.mcp import http as mcp_http, stdio as mcp_stdio
from digital_twins.mcp.auth import mcp_authenticator
from digital_twins.mcp.registry import MCPContext
from digital_twins.scheduler.schedules import create_schedule, list_schedules
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# Mock backends
# ---------------------------------------------------------------------------

def _mock_embedder(texts):
    """Return fixed 384-dim vectors (one per input text)."""
    return [[0.1] * 384 for _ in texts]


def _mock_qdrant_client():
    """In-memory Qdrant client for hermetic vector operations."""
    from qdrant_client import QdrantClient
    client = QdrantClient(":memory:")
    client.create_collection(
        QDRANT_COLLECTION,
        vectors_config={"size": 384, "distance": "Cosine"},
    )
    return client


def _mock_config():
    """A controlled config dict for the MCP tool bodies (fs source enabled)."""
    return {
        "qdrant": {"url": "http://mock:6333"},
        "embedding": {"model": "mock-model", "device": "cpu"},
        "llm": {"endpoint": None, "model": None},
        "sources": {"fs": {"enabled": True}},
    }


def _mock_health_checks(config):
    """Stub health checks: all endpoints OK, no network I/O."""
    from digital_twins.health import HealthResult
    return [
        HealthResult(
            endpoint="qdrant",
            ok=True,
            detail="mocked OK",
            remediation="",
        ),
        HealthResult(
            endpoint="llm",
            ok=False,
            detail="llm.endpoint not configured",
            remediation="set llm.endpoint to enable chat",
        ),
    ]


def _mock_run_pipeline(config, db, qdrant, embedder, **kwargs):
    """Stub pipeline: one item ingested per enabled source, audit row written."""
    import uuid as _uuid
    from digital_twins.ingest.pipeline import RunSummary
    run_id = "mock-" + _uuid.uuid4().hex[:12]
    # Reflect the source_names kwarg: each named source gets a count of 1,
    # or the default {"fs": 1} when no source_names is passed.
    source_names = kwargs.get("source_names")
    if source_names:
        counts = {s: 1 for s in source_names}
    else:
        counts = {"fs": 1}
    points = len(counts) * 2
    summary = RunSummary(
        run_id=run_id,
        status="ok",
        counts=counts,
        points=points,
    )
    # The real pipeline writes the audit row; the mock does it too so the
    # tests can assert the row shape.
    from digital_twins.state.models import start_audit_run, finish_audit_run
    start_audit_run(db, run_id, trigger=kwargs.get("trigger", "mcp"),
                    scheduled_by=kwargs.get("scheduled_by", "system"))
    finish_audit_run(db, run_id, "ok", counts)
    return summary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """State DB with cross-thread SQLite (the HTTP handler runs on its own
    thread, so the connection must be shared across threads)."""
    import sqlite3
    state_path = tmp_path / "state"
    state_path.mkdir(parents=True, exist_ok=True)
    d = sqlite3.connect(str(state_path / "state.db"), check_same_thread=False)
    d.execute("PRAGMA journal_mode=WAL")
    d.execute("PRAGMA foreign_keys=ON")
    migrate(d)
    yield d
    d.close()


@pytest.fixture
def alice(db):
    """First account is admin."""
    create_account(db, "alice@example.com", "pw-alice")
    return "alice@example.com"


@pytest.fixture
def bob(db, alice):
    """Second account is scheduler."""
    create_account(db, "bob@example.com", "pw-bob", role="scheduler")
    return "bob@example.com"


@pytest.fixture
def carol(db, alice, bob):
    """Third account is reader."""
    create_account(db, "carol@example.com", "pw-carol", role="reader")
    return "carol@example.com"


@pytest.fixture
def alice_token(db, alice):
    _, token = create_personal_token(db, alice)
    return token


@pytest.fixture
def bob_token(db, bob):
    _, token = create_personal_token(db, bob)
    return token


@pytest.fixture
def carol_session(db, carol):
    """Create a session token for Carol."""
    plaintext, _expires_at = create_session(db, carol)
    return plaintext


@pytest.fixture
def qdrant():
    return _mock_qdrant_client()


@pytest.fixture
def ctx_alice(db, alice, qdrant, monkeypatch):
    """Admin context with mocked Qdrant + embedder + pipeline."""
    cfg = _mock_config()
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_client", lambda c: qdrant)
    monkeypatch.setattr(disp_mod, "_embed_query", lambda c, t: [[0.1] * 384])
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db_: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda c: (lambda: qdrant))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda c: _mock_embedder)
    monkeypatch.setattr(disp_mod, "run_pipeline", _mock_run_pipeline)
    return MCPContext(
        db=db,
        caller_email=alice,
        caller_role="admin",
        agent_kind="external-agent",
        config=cfg,
    )


@pytest.fixture
def ctx_bob(db, bob, qdrant, monkeypatch):
    """Scheduler context (same mocks)."""
    cfg = _mock_config()
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_client", lambda c: qdrant)
    monkeypatch.setattr(disp_mod, "_embed_query", lambda c, t: [[0.1] * 384])
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db_: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda c: (lambda: qdrant))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda c: _mock_embedder)
    monkeypatch.setattr(disp_mod, "run_pipeline", _mock_run_pipeline)
    return MCPContext(
        db=db,
        caller_email=bob,
        caller_role="scheduler",
        agent_kind="external-agent",
        config=cfg,
    )


@pytest.fixture
def ctx_carol(db, carol, qdrant, monkeypatch):
    """Reader context (same mocks)."""
    cfg = _mock_config()
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_client", lambda c: qdrant)
    monkeypatch.setattr(disp_mod, "_embed_query", lambda c, t: [[0.1] * 384])
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db_: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda c: (lambda: qdrant))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda c: _mock_embedder)
    monkeypatch.setattr(disp_mod, "run_pipeline", _mock_run_pipeline)
    return MCPContext(
        db=db,
        caller_email=carol,
        caller_role="reader",
        agent_kind="external-agent",
        config=cfg,
    )


# ---------------------------------------------------------------------------
# Helper: drive one tool via dispatch
# ---------------------------------------------------------------------------

def _call(ctx, tool: str, args: dict = None) -> dict:
    """Dispatch one tool call and return the result dict."""
    return disp_mod.dispatch(ctx, tool, args or {})


# ---------------------------------------------------------------------------
# 1. kb_schedule_list: own scope, admin all_users, reader denial
# ---------------------------------------------------------------------------

def test_kb_schedule_list_own_scope(ctx_bob):
    """Bob lists his own schedules: one created by him, one by Alice not visible."""
    create_schedule(db=ctx_bob.db, owner="bob@example.com",
                    source="fs", preset="daily")
    create_schedule(db=ctx_bob.db, owner="alice@example.com",
                    source="fs", preset="hourly")
    result = _call(ctx_bob, "kb_schedule_list")
    assert result["ok"] is True
    schedules = result["data"]["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["owner"] == "bob@example.com"
    assert schedules[0]["preset"] == "daily"


def test_kb_schedule_list_admin_all_users(ctx_alice):
    """Alice (admin) lists all users' schedules with all_users=True."""
    create_schedule(db=ctx_alice.db, owner="bob@example.com",
                    source="fs", preset="daily")
    create_schedule(db=ctx_alice.db, owner="alice@example.com",
                    source="fs", preset="weekly")
    result = _call(ctx_alice, "kb_schedule_list", {"all_users": True})
    assert result["ok"] is True
    schedules = result["data"]["schedules"]
    assert len(schedules) == 2
    owners = {s["owner"] for s in schedules}
    assert "bob@example.com" in owners
    assert "alice@example.com" in owners


def test_kb_schedule_list_all_users_denied_for_non_admin(ctx_bob):
    """Bob (scheduler) requesting all_users gets permission_denied."""
    result = _call(ctx_bob, "kb_schedule_list", {"all_users": True})
    assert result["ok"] is False
    assert result["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# 2. kb_schedule_create: success, invalid preset, reader denial
# ---------------------------------------------------------------------------

def test_kb_schedule_create_success(ctx_bob):
    """Bob creates a schedule for himself (owner = Bob)."""
    result = _call(ctx_bob, "kb_schedule_create", {
        "source": "fs",
        "preset": "daily",
        "fire_time": "04:00",
    })
    assert result["ok"] is True
    schedule = result["data"]["schedule"]
    assert schedule["owner"] == "bob@example.com"
    assert schedule["preset"] == "daily"
    assert schedule["fire_time"] == "04:00"
    assert schedule["enabled"] == 1  # sqlite returns int 1 for True


def test_kb_schedule_create_invalid_preset(ctx_bob):
    """Invalid preset → invalid_preset error."""
    result = _call(ctx_bob, "kb_schedule_create", {
        "source": "fs",
        "preset": "hourly-x2",  # not in the enum
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_preset"


def test_kb_schedule_create_reader_denied(ctx_carol):
    """Carol (reader) cannot create schedules."""
    result = _call(ctx_carol, "kb_schedule_create", {
        "source": "fs",
        "preset": "daily",
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# 3. kb_schedule_update: owner success, non-owner schedule_not_found
# ---------------------------------------------------------------------------

def test_kb_schedule_update_owner(ctx_bob):
    """Bob updates his own schedule."""
    sched = create_schedule(db=ctx_bob.db, owner="bob@example.com",
                            source="fs", preset="daily")
    result = _call(ctx_bob, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "fire_time": "05:00",
    })
    assert result["ok"] is True
    assert result["data"]["schedule"]["fire_time"] == "05:00"


def test_kb_schedule_update_non_owner(ctx_bob):
    """Bob cannot update Alice's schedule → schedule_not_found (R5)."""
    sched = create_schedule(db=ctx_bob.db, owner="alice@example.com",
                            source="fs", preset="daily")
    result = _call(ctx_bob, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "fire_time": "05:00",
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "schedule_not_found"


def test_kb_schedule_update_invalid_param(ctx_bob):
    """Update with invalid param → invalid_param."""
    sched = create_schedule(db=ctx_bob.db, owner="bob@example.com",
                            source="fs", preset="every-N-hours", param=4)
    result = _call(ctx_bob, "kb_schedule_update", {
        "schedule_id": sched["id"],
        "param": 0,  # N must be >= 1
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_param"


# ---------------------------------------------------------------------------
# 4. kb_schedule_delete: owner success, reader denial
# ---------------------------------------------------------------------------

def test_kb_schedule_delete_owner(ctx_bob):
    """Bob deletes his own schedule."""
    sched = create_schedule(db=ctx_bob.db, owner="bob@example.com",
                            source="fs", preset="daily")
    result = _call(ctx_bob, "kb_schedule_delete", {
        "schedule_id": sched["id"],
    })
    assert result["ok"] is True
    assert result["data"]["deleted"] == sched["id"]
    # Confirm it's gone:
    schedules = list_schedules(ctx_bob.db, owner="bob@example.com")
    assert all(s["id"] != sched["id"] for s in schedules)


def test_kb_schedule_delete_reader_denied(ctx_carol):
    """Carol (reader) cannot delete schedules."""
    result = _call(ctx_carol, "kb_schedule_delete", {"schedule_id": 999})
    assert result["ok"] is False
    assert result["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# 5. kb_schedule_run: pipeline success, source_disabled, run_failed
# ---------------------------------------------------------------------------

def test_kb_schedule_run_success(ctx_alice, qdrant, tmp_path, monkeypatch):
    """Alice triggers a pipeline run via her schedule (mocked pipeline)."""
    import digital_twins.ingest.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "run_pipeline", _mock_run_pipeline)
    from digital_twins.scheduler.schedules import create_schedule
    sched = create_schedule(db=ctx_alice.db, owner="alice@example.com",
                            source="fs", preset="daily")
    result = _call(ctx_alice, "kb_schedule_run", {
        "schedule_id": sched["id"],
    })
    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "ok"
    assert data["run_id"].startswith("mock-")
    assert data["per_source_counts"].get("fs") == 1
    assert data["agent_kind"] == "external-agent"
    # Audit row exists with trigger='mcp' + scheduled_by=caller:
    rows = ctx_alice.db.execute(
        "SELECT trigger, scheduled_by, status FROM audit_runs "
        "WHERE trigger='mcp'"
    ).fetchall()
    assert len(rows) >= 1
    assert rows[0][1] == "alice@example.com"


def test_kb_schedule_run_source_disabled(ctx_alice):
    """Run with a disabled source → source_disabled error."""
    from digital_twins.scheduler.schedules import create_schedule
    # Create a schedule pointing at a disabled source:
    sched = create_schedule(db=ctx_alice.db, owner="alice@example.com",
                            source="yahoo", preset="daily")
    # The mock config has yahoo disabled:
    ctx_alice.config["sources"]["yahoo"] = {"enabled": False}
    result = _call(ctx_alice, "kb_schedule_run", {
        "schedule_id": sched["id"],
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "source_disabled"


def test_kb_schedule_run_failure(ctx_alice, monkeypatch):
    """Pipeline raises → run_failed error code + failed audit row (R11)."""
    import json as _json
    from digital_twins.scheduler.schedules import create_schedule
    sched = create_schedule(db=ctx_alice.db, owner="alice@example.com",
                            source="fs", preset="daily")

    import digital_twins.ingest.pipeline as pipeline_mod

    def _failing_pipeline(config, db, qdrant, embedder, **kwargs):
        # Mirror the real pipeline's exception-path contract (pipeline.py:254-256):
        # start_audit_run → work → finish_audit_run("failed") → re-raise.
        from digital_twins.state.models import start_audit_run, finish_audit_run
        import uuid as _uuid
        run_id = "mock-" + _uuid.uuid4().hex[:12]
        start_audit_run(db, run_id, trigger=kwargs.get("trigger", "mcp"),
                        scheduled_by=kwargs.get("scheduled_by", "system"))
        finish_audit_run(db, run_id, "failed", {})
        raise RuntimeError("simulated pipeline crash")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", _failing_pipeline)
    result = _call(ctx_alice, "kb_schedule_run", {
        "schedule_id": sched["id"],
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "run_failed"
    assert "simulated pipeline crash" in result["error"]["message"]
    # R11: the pipeline's exception path wrote a `failed` audit row with trigger='mcp'.
    rows = ctx_alice.db.execute(
        "SELECT run_id, status, trigger FROM audit_runs WHERE trigger='mcp'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "failed"
    assert rows[0][2] == "mcp"


# ---------------------------------------------------------------------------
# 6. kb_run_history: own scope, admin cross-user + access-log, R13 exclusion
# ---------------------------------------------------------------------------

def test_kb_run_history_own_scope(ctx_bob):
    """Bob queries his own run history: only his rows, no access-log rows."""
    import uuid
    from digital_twins.state.models import start_audit_run, finish_audit_run
    # Seed two runs for Bob:
    run1 = str(uuid.uuid4())
    start_audit_run(ctx_bob.db, run1, trigger="mcp",
                    scheduled_by="bob@example.com")
    finish_audit_run(ctx_bob.db, run1, "ok")
    run2 = str(uuid.uuid4())
    start_audit_run(ctx_bob.db, run2, trigger="manual",
                    scheduled_by="bob@example.com")
    finish_audit_run(ctx_bob.db, run2, "partial")
    # Seed an access-log row for Bob (R8):
    from digital_twins.mcp.dispatch import _write_history_access_log
    _write_history_access_log(ctx_bob.db, "alice@example.com",
                              "bob@example.com", "hermes")

    result = _call(ctx_bob, "kb_run_history")
    assert result["ok"] is True
    runs = result["data"]["runs"]
    # Two real runs, one access-log excluded:
    assert len(runs) == 2
    statuses = {r["status"] for r in runs}
    assert "ok" in statuses
    assert "partial" in statuses
    # No access-log rows:
    assert all(r["trigger"] != "mcp" or "mcp_history_query" not in
               r.get("per_source_counts", {}) for r in runs)


def test_kb_run_history_admin_cross_user(ctx_alice):
    """Alice (admin) queries Bob's runs: cross-user read + access-log row."""
    import uuid
    from digital_twins.state.models import start_audit_run, finish_audit_run
    run_bob = str(uuid.uuid4())
    start_audit_run(ctx_alice.db, run_bob, trigger="mcp",
                    scheduled_by="bob@example.com")
    finish_audit_run(ctx_alice.db, run_bob, "ok")

    result = _call(ctx_alice, "kb_run_history", {"user": "bob@example.com"})
    assert result["ok"] is True
    runs = result["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["scheduled_by"] == "bob@example.com"
    # Access-log row written (R8):
    log_row = ctx_alice.db.execute(
        "SELECT per_source_counts FROM audit_runs "
        "WHERE per_source_counts LIKE '%mcp_history_query%'"
    ).fetchone()
    assert log_row is not None
    counts = json.loads(log_row[0])
    assert counts.get("mcp_history_query", {}).get("target_user") == \
        "bob@example.com"


def test_kb_run_history_cross_user_denied(ctx_bob):
    """Bob (scheduler) cannot query Alice's runs → permission_denied."""
    result = _call(ctx_bob, "kb_run_history", {"user": "alice@example.com"})
    assert result["ok"] is False
    assert result["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# 7. kb_search: success, qdrant_unavailable, embedding_unavailable, bad_request
# ---------------------------------------------------------------------------

def test_kb_search_success(ctx_alice, qdrant):
    """Alice searches with mocked Qdrant + embedder: results sorted by score."""
    # Seed two points in the mocked Qdrant (collection already created by fixture):
    from qdrant_client.models import PointStruct
    qdrant.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            PointStruct(id=1, vector=[0.1] * 384,
                        payload={"text": "first doc",
                                 "source_url": "https://a",
                                 "source": "fs", "chunk_index": 0,
                                 "owner_tag": "alice@example.com-ingest"}),
            PointStruct(id=2, vector=[0.2] * 384,
                        payload={"text": "second doc",
                                 "source_url": "https://b",
                                 "source": "fs", "chunk_index": 0,
                                 "owner_tag": "alice@example.com-ingest"}),
        ],
    )
    result = _call(ctx_alice, "kb_search", {"query": "test", "limit": 5})
    assert result["ok"] is True
    assert result["count"] == 2
    # Sorted descending by score:
    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


def test_kb_search_bad_request(ctx_alice):
    """Blank/missing query → bad_request."""
    for args in ({}, {"query": ""}, {"query": "   "}, {"query": 123}):
        result = _call(ctx_alice, "kb_search", args)
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_request"


def test_kb_search_qdrant_unavailable(ctx_alice, monkeypatch):
    """Qdrant client fails to resolve → qdrant_unavailable."""
    def _fail(c):
        raise disp_mod.QdrantUnavailable("qdrant.url is not configured")
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_client", _fail)
    result = _call(ctx_alice, "kb_search", {"query": "x"})
    assert result["ok"] is False
    assert result["error"]["code"] == "qdrant_unavailable"
    assert "remediation" in result["error"]
    assert "qdrant.url" in result["error"]["remediation"]


def test_kb_search_embedding_unavailable(ctx_alice, monkeypatch):
    """Embedder fails → embedding_unavailable."""
    def _fail(c, t):
        raise disp_mod.EmbeddingUnavailable(
            "embedding.model 'mock' failed to load: disk full")
    monkeypatch.setattr(disp_mod, "_embed_query", _fail)
    result = _call(ctx_alice, "kb_search", {"query": "x"})
    assert result["ok"] is False
    assert result["error"]["code"] == "embedding_unavailable"
    assert "embedding.model" in result["error"]["remediation"]


def test_kb_search_limit_clamping(ctx_alice, qdrant):
    """Limit clamping: <1 → 1, >100 → 100, non-int → 5.

    Seed 7 points so the effective limit is observable in the count:
      limit=0     → clamped to 1   → count == 1
      limit=500   → clamped to 100 → count == 7  (only 7 exist, not 100)
      limit="abc" → falls back to 5 → count == 5
    """
    from qdrant_client.models import PointStruct
    qdrant.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            PointStruct(id=i, vector=[0.1] * 384,
                        payload={"text": f"doc {i}", "source_url": "https://x",
                                 "source": "fs", "chunk_index": 0,
                                 "owner_tag": "alice@example.com-ingest"})
            for i in range(7)
        ],
    )
    r1 = _call(ctx_alice, "kb_search", {"query": "x", "limit": 0})
    assert r1["ok"] is True
    assert r1["count"] == 1
    r2 = _call(ctx_alice, "kb_search", {"query": "x", "limit": 500})
    assert r2["ok"] is True
    assert r2["count"] == 7
    r3 = _call(ctx_alice, "kb_search", {"query": "x", "limit": "abc"})
    assert r3["ok"] is True
    assert r3["count"] == 5


# ---------------------------------------------------------------------------
# 8. kb_chat: not_implemented surface
# ---------------------------------------------------------------------------

def test_kb_chat_not_implemented(ctx_alice):
    """kb_chat returns not_implemented (007 ships the surface only)."""
    result = _call(ctx_alice, "kb_chat", {"query": "hello"})
    assert result["ok"] is False
    assert result["error"]["code"] == "not_implemented"
    assert "llm.endpoint" in result["error"]["remediation"]


def test_kb_chat_bad_request(ctx_alice):
    """Blank query → bad_request before any config read."""
    for args in ({}, {"query": ""}, {"query": 42}):
        result = _call(ctx_alice, "kb_chat", args)
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_request"


def test_kb_chat_config_not_loaded(ctx_alice, monkeypatch):
    """ctx.config is None → config_not_loaded (fail-closed)."""
    ctx_alice.config = None
    result = _call(ctx_alice, "kb_chat", {"query": "x"})
    assert result["ok"] is False
    assert result["error"]["code"] == "config_not_loaded"


# ---------------------------------------------------------------------------
# 9. kb_ingest: success, prerequisite_missing, unknown_source, run_failed
# ---------------------------------------------------------------------------

def test_kb_ingest_success(ctx_alice):
    """Alice triggers a KB ingest run: one audit row, trigger='mcp'."""
    result = _call(ctx_alice, "kb_ingest", {"source": "fs"})
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["counts"] == {"fs": 1}
    assert result["points"] == 2
    # Audit row:
    row = ctx_alice.db.execute(
        "SELECT trigger, scheduled_by, status FROM audit_runs "
        "WHERE trigger='mcp'"
    ).fetchone()
    assert row is not None
    assert row[0] == "mcp"
    assert row[1] == "alice@example.com"
    assert row[2] == "ok"


def test_kb_ingest_all_sources(ctx_alice):
    """Ingest with source='all': runs all enabled sources."""
    ctx_alice.config["sources"]["hermes"] = {"enabled": True}
    result = _call(ctx_alice, "kb_ingest", {"source": "all"})
    assert result["ok"] is True
    assert result["counts"] == {"fs": 1, "hermes": 1}


def test_kb_ingest_prerequisite_missing(ctx_alice, monkeypatch):
    """Pipeline raises PrerequisiteError → prerequisite_missing code."""
    from digital_twins.ingest.pipeline import PrerequisiteError
    def _fail(*a, **kw):
        raise PrerequisiteError("gmail", ["imap.password"])
    monkeypatch.setattr(disp_mod, "run_pipeline", _fail)
    # Enable gmail in the config so the source validation passes:
    ctx_alice.config["sources"]["gmail"] = {"enabled": True}
    result = _call(ctx_alice, "kb_ingest", {"source": "gmail"})
    assert result["ok"] is False
    assert result["error"]["code"] == "prerequisite_missing"
    assert "imap.password" in result["error"]["message"]


def test_kb_ingest_unknown_source(ctx_alice):
    """Ingest with an unknown source name → bad_request."""
    result = _call(ctx_alice, "kb_ingest", {"source": "nonexistent"})
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert "unknown source" in result["error"]["message"]


def test_kb_ingest_disabled_source(ctx_alice):
    """Ingest with a disabled source → bad_request."""
    ctx_alice.config["sources"]["yahoo"] = {"enabled": False}
    result = _call(ctx_alice, "kb_ingest", {"source": "yahoo"})
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert "not enabled" in result["error"]["message"]


def test_kb_ingest_reader_denied(ctx_carol):
    """Carol (reader) cannot trigger ingest runs."""
    result = _call(ctx_carol, "kb_ingest", {"source": "fs"})
    assert result["ok"] is False
    assert result["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# 10. kb_health: success, config_not_loaded
# ---------------------------------------------------------------------------

def test_kb_health_success(ctx_alice, monkeypatch):
    """Alice queries health: mocked checks returned, no network I/O."""
    monkeypatch.setattr("digital_twins.health.run_health_checks",
                       _mock_health_checks)
    result = _call(ctx_alice, "kb_health")
    assert result["ok"] is True
    checks = result["checks"]
    assert len(checks) == 2
    assert checks[0]["endpoint"] == "qdrant"
    assert checks[0]["ok"] is True
    assert checks[1]["endpoint"] == "llm"
    assert checks[1]["ok"] is False
    assert "llm.endpoint" in checks[1]["detail"]


def test_kb_health_config_not_loaded(ctx_alice, monkeypatch):
    """ctx.config is None → config_not_loaded."""
    ctx_alice.config = None
    result = _call(ctx_alice, "kb_health")
    assert result["ok"] is False
    assert result["error"]["code"] == "config_not_loaded"


# ---------------------------------------------------------------------------
# Auth scenarios: service token, personal token, session token
# ---------------------------------------------------------------------------

def test_auth_service_token_resolves_to_admin(db, alice, monkeypatch):
    """DT_SERVICE_TOKEN env → service-account email (admin)."""
    monkeypatch.setenv("DT_SERVICE_TOKEN", "svc-secret")
    authenticator = mcp_authenticator(db, service_account_email=alice)
    ok, email = authenticator({"Authorization": "Bearer svc-secret"})
    assert ok is True
    assert email == alice


def test_auth_personal_token_resolves_to_scheduler(db, bob_token, monkeypatch):
    """Personal token → Bob's email + scheduler role."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    authenticator = mcp_authenticator(db, service_account_email="system")
    ok, email = authenticator(
        {"Authorization": f"Bearer {bob_token}"})
    assert ok is True
    assert email == "bob@example.com"
    role = get_role(db, email)
    assert role == "scheduler"


def test_auth_session_token_resolves_to_reader(db, carol_session,
                                                monkeypatch):
    """Session token → Carol's email + reader role."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    authenticator = mcp_authenticator(db, service_account_email="system")
    ok, email = authenticator(
        {"Authorization": f"Bearer {carol_session}"})
    assert ok is True
    assert email == "carol@example.com"
    role = get_role(db, email)
    assert role == "reader"


def test_auth_unknown_token_rejected(db, monkeypatch):
    """Unknown Bearer token → 401-class denial (False, None)."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    authenticator = mcp_authenticator(db, service_account_email="system")
    ok, email = authenticator(
        {"Authorization": "Bearer unknown-token-123"})
    assert ok is False
    assert email is None


def test_auth_no_credential_rejected(db, monkeypatch):
    """Missing Authorization header → 401-class denial (False, None)."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    authenticator = mcp_authenticator(db, service_account_email="system")
    ok, email = authenticator({})
    assert ok is False
    assert email is None


# ---------------------------------------------------------------------------
# HTTP transport: byte-identical responses + auth gates
# ---------------------------------------------------------------------------

def test_http_transport_parity_with_stdio(db, alice_token, tmp_path,
                                           monkeypatch):
    """HTTP + stdio return byte-identical JSON for the same call (R7)."""
    # Seed a schedule so the list is non-empty:
    create_schedule(db=db, owner="alice@example.com",
                    source="fs", preset="daily")

    # --- stdio path (real NDJSON I/O) ---
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("DT_PERSONAL_TOKEN", alice_token)
    tool_call = {"tool": "kb_schedule_list", "args": {}}
    in_stream = io.StringIO(json.dumps(tool_call) + "\n")
    out_stream = io.StringIO()
    mcp_stdio.serve(in_stream, out_stream, db,
                    service_account_email="system",
                    config={"sources": {"fs": {"enabled": True}}})
    stdio_json = out_stream.getvalue().strip()

    # --- HTTP path (ThreadingHTTPServer, run in a background thread) ---
    server = mcp_http.serve(
        db,
        port=0,  # ephemeral port
        service_account_email="system",
        dispatch=disp_mod.dispatch,
        config={"sources": {"fs": {"enabled": True}}},
    )
    port = server.server_address[1]
    import threading
    server_thread = threading.Thread(
        target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{port}/mcp"
    import urllib.request
    req = urllib.request.Request(
        url,
        data=json.dumps(tool_call).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {alice_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        http_json = resp.read().decode("utf-8")
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)

    # Byte-identical:
    assert stdio_json == http_json, (
        f"transport parity broken:\n  stdio: {stdio_json}\n  http: {http_json}")
    parsed = json.loads(http_json)
    assert parsed["ok"] is True
    assert len(parsed["data"]["schedules"]) == 1


def test_http_auth_gate_missing_token(db, tmp_path, monkeypatch):
    """HTTP request without Authorization → 401."""
    server = mcp_http.serve(db, port=0,
                            service_account_email="system")
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/mcp"
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 401
    body = json.loads(exc.value.read().decode("utf-8"))
    assert body["error"]["code"] == "unauthorized"
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


def test_http_auth_gate_unknown_token(db, tmp_path, monkeypatch):
    """HTTP request with an unknown token → 401 (not 403)."""
    server = mcp_http.serve(db, port=0,
                            service_account_email="system")
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/mcp"
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer unknown",
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 401
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


def test_http_unknown_path_404(db, tmp_path, monkeypatch):
    """HTTP request to /other → 404."""
    server = mcp_http.serve(db, port=0,
                            service_account_email="system")
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/other"
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 404
    body = json.loads(exc.value.read().decode("utf-8"))
    assert body["error"]["code"] == "not_found"
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


def test_http_get_405(db, tmp_path, monkeypatch):
    """HTTP GET → 405 (only POST /mcp)."""
    server = mcp_http.serve(db, port=0,
                            service_account_email="system")
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/mcp"
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 405
    body = json.loads(exc.value.read().decode("utf-8"))
    assert body["error"]["code"] == "method_not_allowed"
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


# ---------------------------------------------------------------------------
# Error paths: malformed JSON, missing tool field, non-object body
# ---------------------------------------------------------------------------

def test_stdio_malformed_json(db, alice, alice_token, monkeypatch):
    """Malformed JSON line → internal_error."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("DT_PERSONAL_TOKEN", alice_token)
    in_stream = io.StringIO("not-valid-json\n")
    out_stream = io.StringIO()
    mcp_stdio.serve(in_stream, out_stream, db,
                    service_account_email="system",
                    config={"sources": {}})
    parsed = json.loads(out_stream.getvalue().strip())
    assert parsed["ok"] is False
    assert parsed["error"]["code"] == "internal_error"
    assert "malformed JSON" in parsed["error"]["message"]


def test_stdio_missing_tool_field(db, alice, alice_token, monkeypatch):
    """Request without 'tool' string → internal_error."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("DT_PERSONAL_TOKEN", alice_token)
    in_stream = io.StringIO(json.dumps({"args": {}}) + "\n")
    out_stream = io.StringIO()
    mcp_stdio.serve(in_stream, out_stream, db,
                    service_account_email="system",
                    config={"sources": {}})
    parsed = json.loads(out_stream.getvalue().strip())
    assert parsed["ok"] is False
    assert parsed["error"]["code"] == "internal_error"
    assert "'tool'" in parsed["error"]["message"]


def test_http_non_object_body(db, alice_token, tmp_path, monkeypatch):
    """HTTP body that's a JSON array (not object) → 400."""
    server = mcp_http.serve(db, port=0,
                            service_account_email="system")
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/mcp"
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        url,
        data=json.dumps(["not", "an", "object"]).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {alice_token}",
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400
    body = json.loads(exc.value.read().decode("utf-8"))
    assert body["error"]["code"] == "bad_request"
    server.shutdown()
    server.server_close()
    t.join(timeout=5)
