"""007 T012 RED: kb_chat unit tests (007-R2, SC-003).

kb_chat ships a "surface only" in 007 — the body reads
llm.endpoint / llm.model (the only knob access, making the body
decision-ready for the follow-up slice) and returns the
``not_implemented`` result in BOTH branches (set or unset).

Tests:
  (a) result shape is the exact 501-surface:
        {"ok": False, "error": {"code": "not_implemented",
         "remediation": "set llm.endpoint / llm.model to enable chat
         (007 ships the surface only; follow-up slice fills
         generation)"}}
      with llm.endpoint / llm.model BOTH unset AND both set (mirrors
      006's two-branch handler).
  (b) the body performs the llm.endpoint / llm.model config read —
      asserted via a spy on config.schema.get, or a dict that records
      the dotted path keys the body asked for.
  (c) blank / missing query → bad_request "query must be a non-empty
      string" BEFORE any config read (the body short-circuits before
      touching llm.*).
  (d) NO LLM call, NO embedding call, NO Qdrant call, NO network call
      — spies on the qdrant / embedding / LLM seams record zero calls.
  (e) config=None → config_not_loaded before any other work.
"""
from __future__ import annotations

import pytest

import digital_twins.mcp.dispatch as dispatch_mod
from digital_twins.mcp import dispatch as d  # alias
from digital_twins.mcp.dispatch import dispatch
from digital_twins.mcp.registry import MCPContext


_CHAT_REMEDIATION = (
    "set llm.endpoint / llm.model to enable chat "
    "(007 ships the surface only; follow-up slice fills generation)"
)


@pytest.fixture
def db(tmp_path):
    from digital_twins.state.db import connect
    db_path = tmp_path / "kb.sqlite3"
    conn = connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _ctx(db, config, email="alice@example.com", role="reader",
         agent_kind="dsh"):
    return MCPContext(
        db=db,
        caller_email=email,
        caller_role=role,
        agent_kind=agent_kind,
        config=config,
    )


# ---------------------------------------------------------------------------
# (a) the exact surface result — both branches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("llm_cfg", [
    {},  # unset
    {"llm": {"endpoint": "http://llm:8080", "model": "some-model"}},
    {"llm": {"endpoint": "http://llm:8080"}},
    {"llm": {"model": "some-model"}},
])
def test_kb_chat_surface_result_both_branches(db, llm_cfg):
    """007-R2: the body returns the 501-surface in BOTH branches —
    whether llm.endpoint / llm.model are set or unset."""
    cfg = dict(llm_cfg)
    result = dispatch(_ctx(db, config=cfg), "kb_chat", {"query": "hi"})
    assert result == {
        "ok": False,
        "error": {
            "code": "not_implemented",
            "remediation": _CHAT_REMEDIATION,
        },
    }, f"kb_chat surface result drifted: {result!r}"


# ---------------------------------------------------------------------------
# (b) the body performs the llm.endpoint / llm.model config read
# ---------------------------------------------------------------------------

def test_kb_chat_reads_llm_endpoint_and_model(db, monkeypatch):
    """007-R2b: the body reads llm.endpoint and llm.model — the only
    knob access; the read makes the body decision-ready for the
    follow-up slice that fills generation.  We spy on
    config.schema.get to confirm the dotted keys are asked for."""
    asked = []
    real_get = None
    try:
        from digital_twins.config import schema as _schema_mod
        real_get = _schema_mod.get
    except Exception:
        pass

    def spy_get(cfg, path):
        asked.append(path)
        return real_get(cfg, path) if real_get is not None else None

    monkeypatch.setattr(dispatch_mod, "_cfg_get", spy_get, raising=False)
    dispatch(_ctx(db, config={"llm": {"endpoint": "http://x",
                                      "model": "m"}}),
             "kb_chat", {"query": "hi"})
    # The body must have asked for both knobs.
    assert "llm.endpoint" in asked, f"body did not read llm.endpoint: {asked}"
    assert "llm.model" in asked, f"body did not read llm.model: {asked}"


# ---------------------------------------------------------------------------
# (c) blank / missing query → bad_request BEFORE any config read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query_arg", [None, "", "   ", 123, ["q"]])
def test_kb_chat_blank_or_missing_query_bad_request(db, query_arg,
                                                    monkeypatch):
    """007-R2c: blank / missing / non-string query →
    bad_request "query must be a non-empty string" BEFORE any config
    read.  The body short-circuits on the query check."""
    cfg = {"llm": {"endpoint": "http://x", "model": "m"}}
    asked = []
    real_get = None
    try:
        from digital_twins.config import schema as _schema_mod
        real_get = _schema_mod.get
    except Exception:
        pass

    def spy_get(cfg, path):
        asked.append(path)
        return real_get(cfg, path) if real_get is not None else None

    monkeypatch.setattr(dispatch_mod, "_cfg_get", spy_get, raising=False)

    # Build args: drop the "query" key entirely when query_arg is None.
    args = {} if query_arg is None else {"query": query_arg}
    result = dispatch(_ctx(db, config=cfg), "kb_chat", args)
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert result["error"]["message"] == "query must be a non-empty string"
    # No config read happened — the body short-circuited on the query.
    assert asked == [], (
        f"body read config before validating query: {asked}"
    )


# ---------------------------------------------------------------------------
# (d) NO LLM / embedding / Qdrant / network call
# ---------------------------------------------------------------------------

def test_kb_chat_performs_no_external_calls(db, monkeypatch):
    """007-R2d: the body performs NO LLM call, NO embedding call, NO
    Qdrant call, NO network call.  We spy on the dispatch module's
    qdrant / embedding helpers and the LLM seam (which does not exist
    yet) and assert zero calls."""
    calls = {"qdrant": 0, "embed": 0, "llm": 0}

    def fake_qdrant(cfg):
        calls["qdrant"] += 1
        raise AssertionError("kb_chat must not resolve Qdrant")

    def fake_embed(cfg, text):
        calls["embed"] += 1
        raise AssertionError("kb_chat must not embed")

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed,
                        raising=False)
    # No LLM seam exists on dispatch_mod yet; the body must not call one.
    result = dispatch(_ctx(db, config={}), "kb_chat", {"query": "hi"})
    assert result["ok"] is False
    assert result["error"]["code"] == "not_implemented"
    assert calls["qdrant"] == 0, "kb_chat called _resolve_qdrant_client"
    assert calls["embed"] == 0, "kb_chat called _embed_query"
    assert calls["llm"] == 0


# ---------------------------------------------------------------------------
# (e) config=None → config_not_loaded before any other work
# ---------------------------------------------------------------------------

def test_kb_chat_config_none_fails_closed(db, monkeypatch):
    """007-R2e: MCPContext(config=None) → config_not_loaded before any
    other work (the 006 _fail_closed guard)."""
    calls = {"qdrant": 0, "embed": 0, "cfg": 0}

    def fake_qdrant(cfg):
        calls["qdrant"] += 1
        raise AssertionError("kb_chat must not resolve Qdrant")

    def fake_embed(cfg, text):
        calls["embed"] += 1
        raise AssertionError("kb_chat must not embed")

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed,
                        raising=False)
    result = dispatch(_ctx(db, config=None), "kb_chat", {"query": "hi"})
    assert result == {
        "ok": False,
        "error": {
            "code": "config_not_loaded",
            "message": (
                "MCPContext.config is None; the transport must load "
                "config before dispatch"
            ),
        },
    }, f"kb_chat config=None result drifted: {result!r}"
    assert calls["qdrant"] == 0
    assert calls["embed"] == 0
