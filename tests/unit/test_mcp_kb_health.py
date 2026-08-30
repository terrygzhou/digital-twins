"""007 T016 RED: kb_health unit tests (007-R4, SC-005).

kb_health runs the package's health checks and returns their
results.  The body mirrors 006's ``_handle_health``: fail-closed
guard → ``_health_mod.run_health_checks(ctx.config)`` (a
module-attribute seam so the tests can monkeypatch the function
without touching the real Qdrant / Neo4j / LLM) → map each
``HealthResult`` to ``{endpoint, ok, detail, remediation}`` →
``{"ok": True, "checks": [...]}``.

Tests:
  (a) result shape: ``{"ok": True, "checks": [{endpoint, ok, detail,
      remediation}]}`` with one entry per fake, field shape and order
      preserved.
  (b) the fake ``run_health_checks`` is called exactly once with
      ``ctx.config`` as its sole argument; NO qdrant / embedding /
      network call (spies on the qdrant / embed seams record zero
      calls).
  (c) ``config=None`` → ``config_not_loaded`` before any other work
      (the fake records zero calls).
"""
from __future__ import annotations

import pytest

import digital_twins.mcp.dispatch as dispatch_mod
from digital_twins.mcp.dispatch import dispatch
from digital_twins.mcp.registry import MCPContext
from digital_twins.health import HealthResult


@pytest.fixture
def db(tmp_path):
    from digital_twins.state.db import connect
    conn = connect(str(tmp_path / "kb.sqlite3"))
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


class _HealthChecker:
    """Spy on the module-attribute seam for run_health_checks."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cfg):
        self.calls.append({"cfg": cfg})
        return self.results


# ---------------------------------------------------------------------------
# (a) result shape
# ---------------------------------------------------------------------------

def test_kb_health_success_shape(db, monkeypatch):
    """007-R4a: result is {"ok": True, "checks": [{endpoint, ok,
    detail, remediation}]} with one entry per fake, field shape and
    order preserved."""
    fakes = [
        HealthResult("qdrant", True, "reachable; personal_kb is 384-dim",
                     ""),
        HealthResult("neo4j", False, "unreachable: ConnectionError",
                     "set neo4j.url in kb.local.yml"),
        HealthResult("llm", True, "configured", ""),
    ]
    spy = _HealthChecker(fakes)
    # Monkeypatch the module-attribute seam on the dispatch module.
    # The body reads ``_health_mod.run_health_checks`` where
    # ``_health_mod`` is ``digital_twins.health`` — we patch
    # digital_twins.health.run_health_checks so the body's read sees
    # the spy.
    import digital_twins.health as health_mod
    monkeypatch.setattr(health_mod, "run_health_checks", spy)

    result = dispatch(_ctx(db, config={"qdrant": {"url": "http://x"}}),
                      "kb_health", {})
    assert result["ok"] is True, f"unexpected: {result!r}"
    checks = result["checks"]
    assert len(checks) == 3, f"expected 3 checks: {checks}"
    # Field shape + order preserved.
    assert checks[0] == {
        "endpoint": "qdrant", "ok": True,
        "detail": "reachable; personal_kb is 384-dim",
        "remediation": "",
    }
    assert checks[1] == {
        "endpoint": "neo4j", "ok": False,
        "detail": "unreachable: ConnectionError",
        "remediation": "set neo4j.url in kb.local.yml",
    }
    assert checks[2] == {
        "endpoint": "llm", "ok": True, "detail": "configured",
        "remediation": "",
    }


def test_kb_health_empty_checks(db, monkeypatch):
    """007-R4a: an empty check list is a valid success —
    {"ok": True, "checks": []}."""
    spy = _HealthChecker([])
    import digital_twins.health as health_mod
    monkeypatch.setattr(health_mod, "run_health_checks", spy)

    result = dispatch(_ctx(db, config={}), "kb_health", {})
    assert result == {"ok": True, "checks": []}, f"drifted: {result!r}"


# ---------------------------------------------------------------------------
# (b) the fake is called exactly once with ctx.config; no other I/O
# ---------------------------------------------------------------------------

def test_kb_health_calls_run_health_checks_once(db, monkeypatch):
    """007-R4b: the fake run_health_checks is called exactly once with
    ctx.config as its sole argument; no qdrant / embedding / network
    call (spies on the qdrant / embed seams record zero calls)."""
    spy = _HealthChecker([HealthResult("qdrant", True, "ok", "")])
    import digital_twins.health as health_mod
    monkeypatch.setattr(health_mod, "run_health_checks", spy)

    # Spy on the qdrant / embed seams — the body must NOT call them.
    calls = {"qdrant": 0, "embed": 0}

    def fake_qdrant(cfg):
        calls["qdrant"] += 1
        raise AssertionError("kb_health must not resolve Qdrant")

    def fake_embed(cfg, text):
        calls["embed"] += 1
        raise AssertionError("kb_health must not embed")

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed,
                        raising=False)

    cfg = {"qdrant": {"url": "http://x"}}
    result = dispatch(_ctx(db, config=cfg), "kb_health", {})
    assert result["ok"] is True
    # Exactly one call, with ctx.config as the sole argument.
    assert len(spy.calls) == 1, (
        f"expected exactly 1 call: {spy.calls}")
    assert spy.calls[0]["cfg"] is cfg, (
        "run_health_checks must be called with ctx.config as the "
        "sole argument")
    # No qdrant / embed / network call.
    assert calls["qdrant"] == 0, "kb_health called _resolve_qdrant_client"
    assert calls["embed"] == 0, "kb_health called _embed_query"


# ---------------------------------------------------------------------------
# (c) config=None → config_not_loaded; the fake records zero calls
# ---------------------------------------------------------------------------

def test_kb_health_config_none_fails_closed(db, monkeypatch):
    """007-R4c: config=None → config_not_loaded before any other
    work (the fake records zero calls)."""
    spy = _HealthChecker([HealthResult("qdrant", True, "ok", "")])
    import digital_twins.health as health_mod
    monkeypatch.setattr(health_mod, "run_health_checks", spy)

    result = dispatch(_ctx(db, config=None), "kb_health", {})
    assert result == {
        "ok": False,
        "error": {
            "code": "config_not_loaded",
            "message": (
                "MCPContext.config is None; the transport must load "
                "config before dispatch"
            ),
        },
    }, f"kb_health config=None result drifted: {result!r}"
    # Zero calls to the fake.
    assert spy.calls == [], (
        f"run_health_checks called on a None-config ctx: {spy.calls}")
