"""008/US1 (T013): the preflight gate is observed at every trigger path.

The failing service is ``embedding`` (endpoint pinned to a closed port —
a real probe, no mock of the failing check); the other three checks are
patched ok so the failure is deterministic and singular.  Contract per
path: non-zero exit / failure surfaced, no successful audit row, the
service name appears in the observable output.
"""
from __future__ import annotations

import http.client
import json

import pytest
import yaml
from click.testing import CliRunner

import digital_twins.health as health_mod
from digital_twins.cli import cli
from digital_twins.config.schema import validate as schema_validate
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

pytestmark = pytest.mark.preflight_real

CLOSED = "http://127.0.0.1:9/v1"  # port 9, refused immediately


def _patch_services(monkeypatch, fail="embedding"):
    for svc in ("qdrant", "neo4j", "llm", "embedding"):
        if svc != fail:
            monkeypatch.setattr(
                health_mod, f"check_{svc}",
                lambda cfg, s=svc: health_mod.HealthResult(
                    s, True, "ok", status="ok"))


def _seed_env(tmp_path, monkeypatch, extra_cfg=None):
    """Fresh config + state dirs; kb.local.yml carries the bad endpoint."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)
    starter = {
        "state_dir": str(state_dir),
        "config_dir": str(config_dir),
        "sources": {},
        "embedding": {"endpoint": CLOSED},
    }
    if extra_cfg:
        starter.update(extra_cfg)
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(starter), encoding="utf-8")
    db = connect(state_dir)
    migrate(db)
    db.close()
    return state_dir


def _seed_schedule(db, source="fs", next_fire_at="2020-01-01T00:00:00"):
    db.execute(
        "INSERT INTO schedules (owner, source, preset, param, fire_time, "
        "enabled, next_fire_at, acl, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("system", source, "daily", None, "03:00", 1,
         next_fire_at, "owner", "2020-01-01T00:00:00", "2020-01-01T00:00:00"),
    )
    db.commit()


def _audit_statuses(db) -> list:
    return [r[0] for r in db.execute(
        "SELECT status FROM audit_runs").fetchall()]


# --- CLI run --once -----------------------------------------------------------

def test_cli_run_once_nonzero_exit_names_service(tmp_path, monkeypatch):
    _seed_env(tmp_path, monkeypatch)
    _patch_services(monkeypatch)
    res = CliRunner().invoke(cli, ["run", "--once"])
    assert res.exit_code == 2, res.output
    assert "embedding" in res.output
    assert "KB_EMBEDDING__ENDPOINT" in res.output


# --- serve startup preflight ----------------------------------------------------

def test_serve_preflight_at_startup_exits_nonzero(tmp_path, monkeypatch):
    _seed_env(tmp_path, monkeypatch)
    _patch_services(monkeypatch)
    import digital_twins.scheduler.loop as loop_mod
    called = {}

    def fake_run_serve(*a, **k):
        called["yes"] = True

    monkeypatch.setattr(loop_mod, "run_serve", fake_run_serve)
    res = CliRunner().invoke(cli, ["serve", "--port", "0"])
    assert res.exit_code == 2, res.output
    assert not called  # preflight must exit before the loop starts
    assert "embedding" in res.output
    assert "KB_EMBEDDING__ENDPOINT" in res.output


# --- scheduler tick -------------------------------------------------------------

def test_scheduler_tick_surfaces_failure_loop_survives(tmp_path, monkeypatch):
    from digital_twins.scheduler.loop import serve_once_tick

    db = connect(tmp_path)
    migrate(db)
    _seed_schedule(db)
    _patch_services(monkeypatch)
    cfg = schema_validate({})
    cfg["sources"]["fs"]["enabled"] = True
    cfg["embedding"]["endpoint"] = CLOSED

    result = serve_once_tick(db, cfg)  # must not raise
    assert result["queue_depth"] == 0  # the fire was consumed
    assert "success" not in _audit_statuses(db)
    assert "failed" in _audit_statuses(db)  # backstop row
    db.close()


# --- MCP dispatch ----------------------------------------------------------------

def test_mcp_ingest_dispatch_error_dict_no_success_audit(tmp_path, monkeypatch):
    from digital_twins.mcp import dispatch
    from digital_twins.mcp.registry import MCPContext

    # dispatch re-resolves config from the env layer — seed it like the CLI
    state_dir = _seed_env(tmp_path, monkeypatch,
                          extra_cfg={"sources": {"fs": {"enabled": True}}})
    db = connect(state_dir)
    migrate(db)
    _seed_schedule(db)
    _patch_services(monkeypatch)
    sched_id = db.execute("SELECT id FROM schedules").fetchone()[0]

    ctx = MCPContext(db, "admin@example.com", "admin", "stdio")
    out = dispatch._kb_schedule_run_body(ctx, {"schedule_id": sched_id})
    assert out["ok"] is False
    assert out["error"]["code"] == "run_failed"
    assert "embedding" in out["error"]["message"]
    assert "KB_EMBEDDING__ENDPOINT" in out["error"]["message"]
    assert "success" not in _audit_statuses(db)
    db.close()


# --- web /api/ingest/run -----------------------------------------------------------

def _http_post(host, port, path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request("POST", path, json.dumps(body), headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            parsed = None
        return resp.status, parsed, raw.decode("utf-8", "replace")
    finally:
        conn.close()


def test_web_ingest_run_preflight_5xx_no_audit(tmp_path, monkeypatch):
    from digital_twins.accounts import create_account
    from digital_twins.web.app import build_web_app, serve

    db = connect(tmp_path)
    migrate(db)
    create_account(db, "admin@example.com", "admin-pw-123")
    db.commit()
    _patch_services(monkeypatch)
    cfg = {
        "state_dir": str(tmp_path),
        "sources": {"fs": {"enabled": True}},
        "embedding": {"endpoint": CLOSED},
    }
    app = build_web_app(db, cfg, host="127.0.0.1", port=0)
    serve(app)
    try:
        host, port = app.server_address[:2]
        code, parsed, raw = _http_post(
            host, port, "/api/auth/signup",
            {"email": "a2@example.com", "password": "pw-123456"})
        assert code == 200, raw[:300]
        code, parsed, raw = _http_post(
            host, port, "/api/auth/signin",
            {"email": "admin@example.com", "password": "admin-pw-123"})
        assert code == 200, raw[:300]
        token = parsed.get("session_token")
        assert token, raw[:300]
        code, parsed, raw = _http_post(
            host, port, "/api/ingest/run", {"source": "fs"}, token=token)
        assert 500 <= code < 600, (code, raw[:300])
        assert "success" not in _audit_statuses(db)
    finally:
        app.shutdown()
        app.server_close()
        db.close()


def test_cli_run_dry_run_skips_preflight_gate(tmp_path, monkeypatch):
    # diff-review P2: --dry-run consumes no service capacity and writes
    # nothing, so the hard-dependency gate intentionally does not apply -
    # a preview must not fail-fast on live services (pre-008 behavior).
    state_dir = _seed_env(tmp_path, monkeypatch)
    db = connect(state_dir)
    migrate(db)
    _patch_services(monkeypatch)  # embedding would fail the gate
    result = CliRunner().invoke(cli, ["run", "--once", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "fail-fast" not in result.output
