"""MCP integration tests (feature 004, T030-T032).

Six end-to-end scenarios covering the full MCP stack:
auth → dispatch → transport → pipeline → audit.

SC-001: fresh client gets the full 10-tool registry (6 real + 4 stubs).
SC-002: non-owner caller gets schedule_not_found for another user's schedule.
SC-003: a successful run writes exactly one audit_runs row (trigger='mcp').
SC-004: a reader is denied on mutating tools (permission_denied).
SC-005: stdio and HTTP transports return byte-identical JSON for the same call.
SC-006: one-record-not-N — the same content ingested via MCP and via the
        001 CLI run path yields exactly one point in Qdrant (constitution II).
"""
from __future__ import annotations

import json
import io

import pytest

from digital_twins.accounts import create_account
from digital_twins.auth import create_personal_token
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.mcp import dispatch as disp_mod
from digital_twins.mcp import stdio as mcp_stdio
from digital_twins.mcp.registry import MCPContext, build_tool_registry
from digital_twins.scheduler.schedules import create_schedule
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir):
    """Build a controlled config dict (fs source enabled, in-memory qdrant)."""
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
    return [[0.5] * 384 for _ in texts]


def _make_db(tmp_path):
    db = connect(tmp_path / "state")
    migrate(db)
    return db


def _ctx(db, email, role, agent_kind="test"):
    return MCPContext(db=db, caller_email=email,
                      caller_role=role, agent_kind=agent_kind)


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fs_dir(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("alpha " * 50, encoding="utf-8")
    return d


@pytest.fixture
def db(tmp_path):
    d = _make_db(tmp_path)
    yield d
    d.close()


# ---------------------------------------------------------------------------
# SC-001: fresh client gets the full 10-tool registry
# ---------------------------------------------------------------------------

def test_sc001_fresh_client_gets_full_tool_list(db):
    """A valid personal-token caller can call all 10 tools; the 4 KB tools
    (the 004 BR-10 stubs, now real in 007) return a stable 007 code (never
    ``not_implemented_yet``); the 6 real tools succeed."""
    # The first account is admin.
    create_account(db, "admin@example.com", "pw-admin")
    _, token = create_personal_token(db, "admin@example.com")

    # Build a context as the admin (the token authenticates to admin's email).
    # The KB tool bodies (007) read ``ctx.config``; the transport normally
    # populates it.  For this registry-shape check we carry a minimal config
    # so the bodies exercise their real (non-fail-closed) paths.
    cfg = {
        "qdrant": {"url": "http://127.0.0.1:6333"},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "sources": {"fs": {"enabled": True}},
    }
    ctx = MCPContext(db=db, caller_email="admin@example.com",
                     caller_role="admin", agent_kind="test", config=cfg)

    # The 4 KB tools (the 004 BR-10 stubs, now real in 007) no longer
    # return ``not_implemented_yet``.  Each returns a result whose
    # ``error.code`` (if any) is one of the stable 007 codes: the set of
    # codes observed is a subset of the documented surface, and
    # ``config_not_loaded`` is never returned (the context carries a
    # non-None config).  We seed the dispatch seams so the bodies reach a
    # deterministic code rather than touching the network.
    import digital_twins.mcp.dispatch as disp
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(disp, "_resolve_qdrant_client",
                   lambda c: (_ for _ in ()).throw(
                       type("QdrantUnavailable", (Exception,), {})(
                           "qdrant.url is not configured")),
                   raising=False)
        mp.setattr(disp, "_embed_query",
                   lambda c, t: [0.1] * 384, raising=False)
        mp.setattr(disp, "run_pipeline",
                   lambda *a, **kw: (_ for _ in ()).throw(
                       type("RunFailed", (Exception,), {})(
                           "fake run failure")),
                   raising=False)
        import digital_twins.health as _health
        mp.setattr(_health, "run_health_checks",
                   lambda c: [], raising=False)

        kb_tools = ("kb_search", "kb_chat", "kb_ingest", "kb_health")
        observed_codes = set()
        for tool in kb_tools:
            result = disp_mod.dispatch(ctx, tool, {"query": "x"})
            if result.get("ok"):
                # kb_health with an empty check list succeeds.
                observed_codes.add("ok")
            else:
                code = result["error"]["code"]
                observed_codes.add(code)
                assert code != "not_implemented_yet", (
                    f"{tool}: BR-10 stub code still returned: {code}")
                assert code != "config_not_loaded", (
                    f"{tool}: config was provided; fail-closed not expected")
        assert observed_codes <= {
            "bad_request", "not_implemented", "permission_denied",
            "run_failed", "qdrant_unavailable", "embedding_unavailable",
            "config_not_loaded", "source_disabled", "prerequisite_missing",
            "unknown_source", "ok",
        }, f"unexpected KB tool codes: {observed_codes}"
        # Spot-check the deterministic codes this context produces:
        # kb_search → bad_request (empty query), kb_chat → not_implemented,
        # kb_ingest → run_failed (fake pipeline), kb_health → ok.
        assert disp_mod.dispatch(ctx, "kb_search", {})["error"]["code"] == \
            "bad_request"
        assert disp_mod.dispatch(ctx, "kb_chat", {"query": "hi"})[
            "error"]["code"] == "not_implemented"
        assert disp_mod.dispatch(ctx, "kb_health", {})["ok"] is True
    finally:
        mp.undo()

    # The 6 real tools must succeed (list/create are safe no-ops on an
    # empty DB; update/delete/run/history need no schedule to be callable —
    # update/delete on a non-existent id will error, so we test list + history
    # + create which are safe, and verify the tool is *registered* for the
    # rest by checking the registry).
    registry = build_tool_registry()
    registry_names = {t["name"] for t in registry}
    expected_real = {
        "kb_schedule_list", "kb_schedule_create", "kb_schedule_update",
        "kb_schedule_delete", "kb_schedule_run", "kb_run_history",
    }
    expected_stubs = {"kb_search", "kb_chat", "kb_ingest", "kb_health"}
    assert registry_names == expected_real | expected_stubs, (
        f"registry must have exactly 10 tools (6 real + 4 stubs), "
        f"got {sorted(registry_names)}"
    )

    # kb_schedule_list on an empty DB → ok, empty list.
    result = disp_mod.dispatch(ctx, "kb_schedule_list", {})
    assert result.get("ok") is True
    assert result["data"]["schedules"] == []

    # kb_run_history on an empty DB → ok, empty runs.
    result = disp_mod.dispatch(ctx, "kb_run_history", {})
    assert result.get("ok") is True
    assert result["data"]["runs"] == []

    # kb_schedule_create → ok, creates a schedule.
    result = disp_mod.dispatch(ctx, "kb_schedule_create", {
        "source": "fs", "preset": "daily",
    })
    assert result.get("ok") is True
    assert "schedule" in result["data"]

    # Verify the token authenticates correctly through mcp_authenticator.
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db, service_account_email="system")
    ok, email = auth({"Authorization": f"Bearer {token}"})
    assert ok is True
    assert email == "admin@example.com"


# ---------------------------------------------------------------------------
# SC-002: non-owner gets schedule_not_found
# ---------------------------------------------------------------------------

def test_sc002_non_owner_gets_schedule_not_found(db):
    """User B (non-admin) calling kb_schedule_run with user A's schedule_id
    gets schedule_not_found (R5: owner-scoping maps denial to not-found).

    Note: the role-gate (R4) fires BEFORE the owner-scope check (R5).
    A reader calling kb_schedule_run gets permission_denied from the
    role-gate.  A *scheduler* (who has trigger_run but isn't admin)
    calling another user's schedule gets schedule_not_found from R5.
    """
    create_account(db, "alice@example.com", "pw")       # admin (first)
    create_account(db, "bob@example.com", "pw",
                   role="scheduler")                    # scheduler

    # Alice (admin) creates a schedule.
    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")

    # Bob (scheduler, non-admin) tries to run Alice's schedule.
    # Role-gate passes (scheduler has trigger_run), owner-scope fails
    # (Bob doesn't own the schedule and isn't admin) → schedule_not_found.
    ctx = _ctx(db, "bob@example.com", "scheduler", agent_kind="test")
    result = disp_mod.dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
    })
    assert result.get("ok") is False
    assert result["error"]["code"] == "schedule_not_found", (
        f"non-owner scheduler: expected schedule_not_found, "
        f"got {result['error']['code']}"
    )

    # Same for update and delete.
    for tool in ("kb_schedule_update", "kb_schedule_delete"):
        result = disp_mod.dispatch(ctx, tool, {"schedule_id": sched["id"]})
        assert result.get("ok") is False
        assert result["error"]["code"] == "schedule_not_found"


# ---------------------------------------------------------------------------
# SC-003: audit row written on a successful run
# ---------------------------------------------------------------------------

def test_sc003_audit_row_written(db, tmp_path, fs_dir, monkeypatch):
    """A successful kb_schedule_run writes exactly one audit_runs row with
    trigger='mcp', scheduled_by=<caller>, and agent_kind in per_source_counts."""
    from qdrant_client import QdrantClient

    create_account(db, "alice@example.com", "pw")  # admin
    in_memory = QdrantClient(":memory:")
    cfg = _cfg(tmp_path, fs_dir)

    # Wire the controlled config + qdrant + embedder into the MCP run body.
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db_: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda c: (lambda: in_memory))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda c: _embedder)

    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    ctx = _ctx(db, "alice@example.com", "admin", agent_kind="hermes")
    result = disp_mod.dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
    })
    assert result.get("ok") is True, f"run: {result}"
    assert result["data"]["status"] == "ok"

    # Exactly one audit row.
    rows = db.execute(
        "SELECT trigger, scheduled_by, status, per_source_counts "
        "FROM audit_runs"
    ).fetchall()
    assert len(rows) == 1, f"one audit row expected, got {len(rows)}"
    trigger, scheduled_by, status, counts_json = rows[0]
    assert trigger == "mcp"
    assert scheduled_by == "alice@example.com"
    assert status == "ok"
    counts = json.loads(counts_json) if counts_json else {}
    assert counts.get("agent_kind") == "hermes"
    assert counts.get("fs") == 1  # one item (a.txt) ingested


# ---------------------------------------------------------------------------
# SC-004: reader denied on mutating tools
# ---------------------------------------------------------------------------

def test_sc004_reader_denied_mutating(db):
    """A reader calling kb_schedule_create or kb_schedule_run gets
    permission_denied (R4: role-gate)."""
    create_account(db, "admin@example.com", "pw")   # admin (first)
    create_account(db, "reader@example.com", "pw",
                   role="reader")

    ctx = _ctx(db, "reader@example.com", "reader", agent_kind="test")

    # kb_schedule_create requires schedule_crud (reader lacks it).
    result = disp_mod.dispatch(ctx, "kb_schedule_create", {
        "source": "fs", "preset": "daily",
    })
    assert result.get("ok") is False
    assert result["error"]["code"] == "permission_denied", (
        f"create: expected permission_denied, "
        f"got {result['error']['code']}"
    )

    # kb_schedule_run requires trigger_run (reader lacks it).
    # (No schedule exists for the reader, but the role-gate fires first.)
    result = disp_mod.dispatch(ctx, "kb_schedule_run", {
        "schedule_id": 9999,
    })
    assert result.get("ok") is False
    assert result["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# SC-005: transport parity (stdio vs HTTP)
# ---------------------------------------------------------------------------

def test_sc005_transport_parity(db):
    """Calling kb_schedule_list via the stdio handler and the HTTP handler
    returns byte-identical JSON (R7 parity by construction: both delegate
    to the same dispatch).

    The stdio transport uses a real NDJSON I/O loop.  The HTTP transport's
    request handler is tested at the dispatch level: both transports
    resolve the same caller identity and call the same dispatch function,
    so the response dict is identical.  We compare the raw JSON strings
    (byte-for-byte) to verify parity.
    """
    create_account(db, "admin@example.com", "pw")  # admin
    create_account(db, "bob@example.com", "pw", role="scheduler")
    _, bob_token = create_personal_token(db, "bob@example.com")

    # Create a schedule owned by Bob so the list is non-empty.
    create_schedule(db, owner="bob@example.com", source="fs", preset="daily")

    tool_call = {"tool": "kb_schedule_list", "args": {}}

    # --- stdio path: real NDJSON I/O loop ---
    import os
    old_token = os.environ.get("DT_PERSONAL_TOKEN")
    os.environ["DT_PERSONAL_TOKEN"] = bob_token
    try:
        in_stream = io.StringIO(json.dumps(tool_call) + "\n")
        out_stream = io.StringIO()
        mcp_stdio.serve(in_stream, out_stream, db,
                        service_account_email="system")
        stdio_json = out_stream.getvalue().strip()
    finally:
        if old_token is not None:
            os.environ["DT_PERSONAL_TOKEN"] = old_token
        else:
            del os.environ["DT_PERSONAL_TOKEN"]

    # --- HTTP path: dispatch level (same call the HTTP handler makes) ---
    # The HTTP handler's do_POST: authenticates via mcp_authenticator,
    # resolves the caller role, builds MCPContext(agent_kind="http"),
    # and calls dispatch(ctx, tool, args).  We replicate that call here
    # to verify the response is byte-identical to the stdio path.
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db, service_account_email="system")
    ok, email = auth({"Authorization": f"Bearer {bob_token}"})
    assert ok is True
    caller_role = db.execute(
        "SELECT role FROM accounts WHERE email=?", (email,)
    ).fetchone()[0]
    http_ctx = MCPContext(db=db, caller_email=email,
                          caller_role=caller_role, agent_kind="http")
    http_result = disp_mod.dispatch(http_ctx, "kb_schedule_list", {})
    http_json = json.dumps(http_result)

    # Both must produce the same data (the tool result).  The agent_kind
    # differs between transports (stdio vs http) but the tool result
    # (kb_schedule_list) does not carry agent_kind in its response.
    stdio_parsed = json.loads(stdio_json)
    http_parsed = json.loads(http_json)

    # The tool result (data) must be identical.
    assert stdio_parsed.get("ok") == http_parsed.get("ok")
    assert stdio_parsed.get("data") == http_parsed.get("data"), (
        f"transport parity: data differs\n"
        f"  stdio data: {stdio_parsed.get('data')}\n"
        f"  http  data: {http_parsed.get('data')}"
    )
    assert stdio_parsed["ok"] is True
    assert len(stdio_parsed["data"]["schedules"]) == 1

    # The raw JSON for the tool result is byte-identical (same dict,
    # same key order from json.dumps).
    # Note: the stdio path wraps the dispatch result directly; the HTTP
    # path does the same.  Both are json.dumps(dispatch_result).
    assert stdio_json == http_json, (
        f"transport parity: raw JSON differs\n"
        f"  stdio: {stdio_json}\n"
        f"  http:  {http_json}"
    )


# ---------------------------------------------------------------------------
# SC-006: one-record-not-N (constitution II, NFR-1/NFR-14)
# ---------------------------------------------------------------------------

def test_sc006_one_record_not_N(db, tmp_path, fs_dir, monkeypatch):
    """The same content ingested via MCP kb_schedule_run AND via the 001
    CLI run_pipeline path yields exactly ONE point in Qdrant, not two.

    This is the top acceptance check: deterministic point IDs + upserts
    ensure idempotency regardless of the trigger path.
    """
    from qdrant_client import QdrantClient

    create_account(db, "alice@example.com", "pw")  # admin
    in_memory = QdrantClient(":memory:")
    cfg = _cfg(tmp_path, fs_dir)

    # --- Path 1: MCP kb_schedule_run ---
    monkeypatch.setattr(disp_mod, "_resolve_mcp_run_config", lambda db_: cfg)
    monkeypatch.setattr(disp_mod, "_resolve_qdrant_factory",
                        lambda c: (lambda: in_memory))
    monkeypatch.setattr(disp_mod, "_resolve_embedder", lambda c: _embedder)

    sched = create_schedule(db, owner="alice@example.com",
                            source="fs", preset="daily")
    ctx = _ctx(db, "alice@example.com", "admin", agent_kind="hermes")
    result = disp_mod.dispatch(ctx, "kb_schedule_run", {
        "schedule_id": sched["id"],
    })
    assert result.get("ok") is True, f"MCP run: {result}"
    mcp_items = result["data"]["per_source_counts"].get("fs", 0)
    assert mcp_items == 1, (
        f"MCP run should ingest 1 item (a.txt), got {mcp_items}"
    )
    # The item produced 2 chunks → 2 points in Qdrant.
    assert _point_count(in_memory) == 2, (
        f"expected 2 points in Qdrant after MCP run, "
        f"got {_point_count(in_memory)}"
    )

    # --- Path 2: 001 CLI run_pipeline (the direct pipeline call) ---
    # The CLI's `run --once` calls run_pipeline directly with the same
    # resolved config, db, qdrant, and embedder.
    summary = run_pipeline(
        cfg, db, in_memory, _embedder,
        source_names=["fs"],
        trigger="manual",
        scheduled_by="alice@example.com",
        owner="alice@example.com",
    )
    cli_points = summary.counts.get("fs", 0)

    # The second run must produce 0 NEW points (high-water marks already
    # advanced by the MCP run; the deterministic point IDs would dedup even
    # without high-water marks).
    assert cli_points == 0, (
        f"one-record-not-N: second run produced {cli_points} new points, "
        f"expected 0 (content already ingested by MCP)"
    )

    # Total point count in Qdrant: exactly 2 (from the first MCP run),
    # not 4 (which would mean the CLI run duplicated them).
    total = _point_count(in_memory)
    assert total == 2, (
        f"one-record-not-N: Qdrant has {total} points, expected exactly 2 "
        f"(the MCP run's 2 chunks). If the CLI run duplicated them, "
        f"we'd see 4."
    )

    # Two audit rows total: one from the MCP run (trigger='mcp') and one
    # from the CLI run (trigger='manual').
    rows = db.execute(
        "SELECT trigger, status FROM audit_runs ORDER BY started_at"
    ).fetchall()
    assert len(rows) == 2, f"two audit rows expected, got {len(rows)}"
    triggers = [r[0] for r in rows]
    assert "mcp" in triggers
    assert "manual" in triggers
