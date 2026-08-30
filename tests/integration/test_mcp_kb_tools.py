"""007 T019 RED: integration e2e for the four real MCP KB tools
(007-R1..R4, SC-001..SC-007).

End-to-end proof across auth → transport → dispatch → body → audit for the
four BR-10 tools 004 shipped as ``not_implemented_yet`` stubs and 007
replaced with real bodies:

  (a) SC-001/SC-002: service-token auth (``DT_SERVICE_TOKEN``) → stdio
      transport → ``kb_search`` against a fake Qdrant client seeded at the
      dispatch seam.  The owner-scoped filter (``owner_tag`` = the service
      account's tag) is asserted, and a second owner's point never appears.
  (b) SC-004: personal-token auth (``DT_PERSONAL_TOKEN`` for a scheduler) →
      stdio transport → ``kb_ingest`` against a fake ``run_pipeline``
      (seeded at the dispatch module-attr seam).  An ``audit_runs`` row
      exists with ``trigger="mcp"``, ``scheduled_by=<caller-email>``, and
      ``agent_kind`` recorded in ``per_source_counts`` (007-R3d / BR-11.5.3).
  (c) SC-005: ``kb_health`` dispatch returns the per-endpoint check list.
  (d) SC-007: both transports populate ``MCPContext.config`` — a dispatch
      driven through the stdio ``serve`` path and through the http handler
      (on ``127.0.0.1:0``) each sees a non-``None`` config, while a
      directly-constructed ``MCPContext(config=None)`` fails closed with
      ``config_not_loaded``.

Mirrors the 004 ``test_mcp_integration.py`` fixtures (migrated state DB in
``tmp_path``, real account, tokens minted through the 004 auth surface).
The bodies are already implemented (Phase 1/2/3); this task commits the
e2e test that proves them green.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from io import StringIO

import pytest

import digital_twins.mcp.dispatch as disp_mod
from digital_twins.accounts import create_account, owner_tag_for
from digital_twins.auth import create_personal_token
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import RunSummary
from digital_twins.mcp import stdio as mcp_stdio
from digital_twins.mcp.registry import MCPContext
from digital_twins.state.models import start_audit_run, finish_audit_run
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """A migrated state DB with a service account (``system``, reader) and a
    scheduler account (``bob``).  The service account must exist so the
    service-token path resolves a live role; the scheduler holds
    ``trigger_run`` so ``kb_ingest`` passes its capability gate."""
    conn = connect(str(tmp_path / "state"))
    migrate(conn)
    create_account(conn, "system", "pw-system", role="reader")
    create_account(conn, "bob@example.com", "pw-bob", role="scheduler")
    yield conn
    conn.close()


def _cfg(tmp_path):
    """A controlled config: no qdrant.url (so the real health checks never
    open a network connection), embedding pinned, one enabled source."""
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": None, "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": {
            "hermes": {"enabled": True, "max_items": 200, "timeout_s": 1500},
            "pi": {"enabled": False, "max_items": 200, "timeout_s": 1500},
            "dsh": {"enabled": False, "max_items": 200, "timeout_s": 1500},
            "paperclip": {"enabled": False, "max_items": 200, "timeout_s": 1500},
            "yahoo": {"enabled": False, "max_items": 200, "timeout_s": 1500},
            "gmail": {"enabled": False, "max_items": 200, "timeout_s": 1500},
            "fs": {"enabled": False, "max_items": 200, "timeout_s": 1500},
        },
    }


def _ctx(db, email, role, config, agent_kind="test"):
    return MCPContext(db=db, caller_email=email, caller_role=role,
                      agent_kind=agent_kind, config=config)


# ---------------------------------------------------------------------------
# fake Qdrant client (seeded at the dispatch seam)
# ---------------------------------------------------------------------------

class _FakeQueryPoint:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


class _FakeQdrantClient:
    """Captures ``query_points`` args; returns only the rows whose
    ``owner_tag`` matches the filter (so a second owner's point never
    appears)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query_points(self, collection, query=None, query_filter=None,
                     limit=None, with_payload=False, **kw):
        self.calls.append({
            "collection": collection,
            "query": query,
            "query_filter": query_filter,
            "limit": limit,
            "with_payload": with_payload,
        })
        # Honor the owner_tag filter the way the real Qdrant would: only
        # rows whose owner_tag matches the filter's value are returned.
        matched = None
        if query_filter is not None and getattr(query_filter, "must", None):
            conds = getattr(query_filter.must[0], "key", None)
            if conds == "owner_tag":
                matched = query_filter.must[0].match.value
        if matched is None:
            return self.rows
        return [r for r in self.rows
                if (r.payload or {}).get("owner_tag") == matched]


def _fake_embed_query(cfg, text):
    return [0.1] * 384


# ---------------------------------------------------------------------------
# fake run_pipeline (seeded at the dispatch module-attr seam)
# ---------------------------------------------------------------------------

class _RunPipelineFake:
    """Records the call, writes its own ``audit_runs`` row (the way the real
    ``run_pipeline`` does via ``start_audit_run`` / ``finish_audit_run``),
    and returns a canned RunSummary.  The body then stamps ``agent_kind``
    onto that row, so the e2e proves the audit row's provenance end to end."""

    def __init__(self, run_id="run-e2e"):
        self.run_id = run_id
        self.calls = []

    def __call__(self, cfg, db, qdrant, embedder=None, **kwargs):
        self.calls.append({"kwargs": kwargs})
        trigger = kwargs.get("trigger", "mcp")
        scheduled_by = kwargs.get("scheduled_by", "system")
        start_audit_run(db, self.run_id, trigger=trigger,
                        scheduled_by=scheduled_by)
        finish_audit_run(db, self.run_id, "ok", {"hermes": 2})
        return RunSummary(
            run_id=self.run_id,
            counts={"hermes": 2},
            points=2,
            status="ok",
        )


def _audit_row(db, run_id):
    row = db.execute(
        "SELECT run_id, status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(zip(
        ("run_id", "status", "trigger", "scheduled_by",
         "per_source_counts"), row))


def _run_stdio_request(db, tool, args, monkeypatch, service_token=None,
                       personal_token=None, config=None):
    """Drive one request through the stdio ``serve`` loop with the given
    credential env; return the parsed response dict."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    if service_token is not None:
        monkeypatch.setenv("DT_SERVICE_TOKEN", service_token)
    if personal_token is not None:
        monkeypatch.setenv("DT_PERSONAL_TOKEN", personal_token)
    inp = StringIO(json.dumps({"tool": tool, "args": args}) + "\n")
    out = StringIO()
    mcp_stdio.serve(inp, out, db, service_account_email="system",
                    config=config)
    return json.loads(out.getvalue().strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# (a) SC-001/SC-002: service-token kb_search against a fake Qdrant client
# ---------------------------------------------------------------------------

def test_kb_search_service_token_owner_scoped(db, tmp_path, monkeypatch):
    """DT_SERVICE_TOKEN → stdio → kb_search: owner-scoped filter + the exact
    result field set; a second owner's point never appears."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    owner_tag = owner_tag_for("system")
    other_tag = owner_tag_for("bob@example.com")
    my_point = _FakeQueryPoint(
        0.9, {"source_url": "u-mine", "text": "mine", "source": "hermes",
              "chunk_index": 0, "owner_tag": owner_tag})
    other_point = _FakeQueryPoint(
        0.8, {"source_url": "u-other", "text": "other", "source": "hermes",
              "chunk_index": 1, "owner_tag": other_tag})
    client = _FakeQdrantClient(rows=[my_point, other_point])

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(disp_mod, "_resolve_qdrant_client", lambda c: client,
                   raising=False)
        mp.setattr(disp_mod, "_embed_query", _fake_embed_query, raising=False)
        result = _run_stdio_request(
            db, "kb_search", {"query": "find me", "limit": 10},
            monkeypatch, service_token="svc-token-e2e",
            config=_cfg(tmp_path))
    finally:
        mp.undo()

    # (i) the dispatch received the owner-scoped filter on the right
    #     collection.
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["collection"] == QDRANT_COLLECTION
    assert call["query_filter"] == Filter(must=[FieldCondition(
        key="owner_tag", match=MatchValue(value=owner_tag))])
    assert call["with_payload"] is True

    # (ii) the wire result carries the exact field set per row; the filter
    #     returns only the caller's point (the other owner's is dropped).
    assert result["ok"] is True
    assert result["count"] == 1
    for row in result["results"]:
        assert set(row.keys()) == {"score", "source_url", "text", "source",
                                   "chunk_index"}

    # (iii) a second owner's point never appears: the filter's match value
    #     is the service account's tag (bob's tag differs), so the fake
    #     client (which honors the filter) returns only the caller's row.
    assert other_tag != owner_tag
    urls = {row["source_url"] for row in result["results"]}
    assert "u-other" not in urls, (
        f"a second owner's point leaked into the results: {urls}")
    assert urls == {"u-mine"}
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# (b) SC-004: personal-token kb_ingest → audit_runs row
# ---------------------------------------------------------------------------

def test_kb_ingest_personal_token_audit_row(db, tmp_path, monkeypatch):
    """DT_PERSONAL_TOKEN (scheduler) → stdio → kb_ingest: an audit_runs row
    with trigger='mcp', scheduled_by=<caller-email>, and agent_kind recorded
    in per_source_counts."""
    _, token = create_personal_token(db, "bob@example.com")
    fake = _RunPipelineFake(run_id="run-e2e")

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(disp_mod, "run_pipeline", fake, raising=False)
        result = _run_stdio_request(
            db, "kb_ingest", {"source": "hermes"},
            monkeypatch, personal_token=token, config=_cfg(tmp_path))
    finally:
        mp.undo()

    # The hand-off was recorded exactly once, with no agent_kind kwarg.
    assert len(fake.calls) == 1
    kw = fake.calls[0]["kwargs"]
    assert "agent_kind" not in kw, f"agent_kind leaked into run_pipeline: {kw}"
    assert kw.get("trigger") == "mcp"
    assert kw.get("scheduled_by") == "bob@example.com"
    assert kw.get("owner") == "bob@example.com"
    assert kw.get("source_names") == ["hermes"]

    # The body succeeded.
    assert result["ok"] is True
    assert result["run_id"] == "run-e2e"

    # The pipeline's own audit row (the fake pipeline is what the real one
    # does: start_audit_run + finish_audit_run) has trigger='mcp',
    # scheduled_by=<caller>, and the body stamped agent_kind onto it.
    row = _audit_row(db, "run-e2e")
    assert row is not None, "no audit_runs row written for the kb_ingest run"
    assert row["trigger"] == "mcp"
    assert row["scheduled_by"] == "bob@example.com"
    counts = json.loads(row["per_source_counts"] or "{}")
    assert counts.get("agent_kind") == "stdio", (
        f"agent_kind not stamped on the audit row: {counts}")


# ---------------------------------------------------------------------------
# (c) SC-005: kb_health returns the per-endpoint check list
# ---------------------------------------------------------------------------

def test_kb_health_per_endpoint_check_list(db, tmp_path):
    """kb_health dispatch → {"ok": True, "checks": [...]} with one entry per
    endpoint (qdrant, neo4j, llm) in the stable field shape."""
    cfg = _cfg(tmp_path)
    ctx = _ctx(db, "system", "reader", cfg, agent_kind="test")
    result = disp_mod.dispatch(ctx, "kb_health", {})
    assert result["ok"] is True
    checks = result["checks"]
    assert [c["endpoint"] for c in checks] == [
        "qdrant", "neo4j", "llm", "embedding"]  # 008 US1: 4 hard deps
    for c in checks:
        assert set(c.keys()) == {"endpoint", "ok", "detail", "remediation"}
    # With qdrant.url / neo4j.url / llm.endpoint all unconfigured, those
    # three report ok=False (real run_health_checks, no network). embedding
    # is host-dependent (in-process model available here), so it is asserted
    # for presence, not for ok.
    by_ep = {c["endpoint"]: c for c in checks}
    assert all(by_ep[e]["ok"] is False for e in ("qdrant", "neo4j", "llm"))


# ---------------------------------------------------------------------------
# (d) SC-007: both transports populate MCPContext.config
# ---------------------------------------------------------------------------

def test_stdio_serve_populates_config_not_none(db, tmp_path, monkeypatch):
    """A dispatch driven through the stdio ``serve`` path sees a non-None
    config on the MCPContext (the KB bodies read it)."""
    seen_ctx = []

    def fake_dispatch(ctx, tool_name, args):
        seen_ctx.append(ctx)
        return {"ok": True}

    inp = StringIO(json.dumps({"tool": "kb_schedule_list", "args": {}}) + "\n")
    out = StringIO()
    mcp_stdio.serve(inp, out, db, service_account_email="system",
                    config=_cfg(tmp_path), dispatch=fake_dispatch)
    assert len(seen_ctx) == 1
    assert seen_ctx[0].config is not None


def test_http_handler_populates_config_not_none(db, tmp_path, monkeypatch):
    """A dispatch driven through the http handler (on 127.0.0.1:0) sees a
    non-None config on the MCPContext."""
    import http.client
    from http.server import ThreadingHTTPServer
    from digital_twins.mcp import http as http_mod
    from digital_twins.state.db import state_db_path
    from digital_twins.state.migrations import migrate

    seen_ctx = []

    def fake_dispatch(ctx, tool_name, args):
        seen_ctx.append(ctx)
        return {"ok": True}

    # The http handler runs on a ThreadingHTTPServer worker thread; SQLite
    # connections are per-thread by default (check_same_thread=True on the
    # ``db`` fixture's connection).  Open a thread-safe connection to the
    # SAME db file so the handler thread can authenticate + resolve roles.
    http_db = sqlite3.connect(
        str(state_db_path(tmp_path / "state")), check_same_thread=False)
    migrate(http_db)

    # The http handler authenticates per-request (service token first).
    monkeypatch.setenv("DT_SERVICE_TOKEN", "svc-http-e2e")

    handler_cls = http_mod.build_handler(http_db, service_account_email="system",
                                         dispatch=fake_dispatch,
                                         config=_cfg(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = server.server_address[:2]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        payload = json.dumps({"tool": "kb_schedule_list", "args": {}}).encode()
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/mcp", body=payload,
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer svc-http-e2e"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)
        http_db.close()
    assert len(seen_ctx) == 1
    assert seen_ctx[0].config is not None
    assert json.loads(body)["ok"] is True


def test_direct_mcp_context_config_none_fails_closed(db, tmp_path):
    """SC-007: a directly-constructed MCPContext(config=None) fails closed
    with config_not_loaded on a KB tool body (before any other work)."""
    ctx = MCPContext(db=db, caller_email="system", caller_role="reader",
                     agent_kind="test", config=None)
    for tool in ("kb_search", "kb_chat", "kb_ingest", "kb_health"):
        result = disp_mod.dispatch(ctx, tool, {})
        assert result["ok"] is False, f"{tool}: expected failure, got {result}"
        assert result["error"]["code"] == "config_not_loaded", (
            f"{tool}: expected config_not_loaded, "
            f"got {result['error']['code']}")
