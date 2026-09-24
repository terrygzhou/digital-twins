"""channels-config T3: web admin /api/config/channels GET+POST (RED-first).

Mirrors the services-panel pattern in tests/integration/test_web_config_api.py
but lives under tests/unit/ (brief: tests/unit/test_web_channels_api.py).
Harness: same build_web_app + http.client pattern as test_web_app_auth.py.
"""
from __future__ import annotations

import http.client
import json
import socket
import time
from pathlib import Path

import pytest
import yaml

from digital_twins.accounts import create_account
from digital_twins.auth import create_session
from digital_twins.state import db as state_db
from digital_twins.web.app import build_web_app, serve

# Obviously-fake credential value (FR-004 safe).
YMAIL_SECRET = "sk-fake-yahoo-app-password"

_READY_TIMEOUT_S = 5.0


# --- helpers ----------------------------------------------------------------

def _make_db(tmp_path: Path):
    return state_db.connect(tmp_path)


def _wait_server_ready(server, timeout=_READY_TIMEOUT_S):
    host, port = server.server_address[:2]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server at {host}:{port} not ready within {timeout}s")


def _http_get(host, port, path, headers=None):
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = None
        return resp.status, parsed, raw
    finally:
        conn.close()


def _http_post(host, port, path, body, headers=None):
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        conn.request("POST", path, body=json.dumps(body), headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = None
        return resp.status, parsed, raw
    finally:
        conn.close()


# --- fixture ----------------------------------------------------------------

@pytest.fixture
def channels_web(tmp_path, monkeypatch):
    """WebApp with admin + reader accounts and an isolated config dir.

    Yields (app, db, host, port, admin_token, reader_token, config_dir).
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Pre-existing kb.local.yml: fs enabled + chunking block (must survive
    # a POST merge_write).
    local_yaml = {
        "sources": {"fs": {"enabled": True, "max_items": 42}},
        "chunking": {"max_chars": 800, "overlap": 100},
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(local_yaml, sort_keys=False), encoding="utf-8")

    # Pre-existing kb.yml (must NOT be touched by any POST).
    (config_dir / "kb.yml").write_text(
        yaml.safe_dump({"llm": {"model": "test-model"}}, sort_keys=False),
        encoding="utf-8")

    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))

    db = _make_db(tmp_path)
    create_account(db, "admin@example.com", "admin-pw-123", role="admin")
    create_account(db, "reader@example.com", "reader-pw-123", role="reader")
    db.commit()
    admin_token = create_session(db, "admin@example.com")[0]
    reader_token = create_session(db, "reader@example.com")[0]
    db.commit()

    cfg = {"state_dir": str(tmp_path), "sources": {},
           "config_dir": str(config_dir)}
    app = build_web_app(db, cfg, host="127.0.0.1", port=0)
    serve(app)
    host, port = app.server_address[:2]
    _wait_server_ready(app)

    try:
        yield app, db, host, port, admin_token, reader_token, config_dir
    finally:
        app.shutdown()
        app.server_close()
        db.close()


# =============================================================================
# GET /api/config/channels
# =============================================================================

def test_get_channels_admin_200_shape(channels_web):
    """GET as admin → 200 with sources dict + env_overrides list."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web

    code, parsed, raw = _http_get(
        host, port, "/api/config/channels",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:400]!r}"
    assert parsed is not None, f"body must be JSON: {raw[:400]!r}"
    assert "sources" in parsed, f"'sources' key missing: {list(parsed.keys())!r}"
    assert "env_overrides" in parsed, f"'env_overrides' key missing"
    sources = parsed["sources"]
    # All 7 built-in sources present
    for name in ("hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs"):
        assert name in sources, f"source {name!r} missing from view: {list(sources.keys())!r}"
        row = sources[name]
        assert "enabled" in row, f"{name}: 'enabled' missing"
        assert "max_items" in row, f"{name}: 'max_items' missing"
        assert "timeout_s" in row, f"{name}: 'timeout_s' missing"
        assert "credential_set" in row, f"{name}: 'credential_set' missing"
        assert isinstance(row["credential_set"], bool), f"{name}: credential_set must be bool"
        assert isinstance(row["prerequisites"], list), f"{name}: prerequisites must be a list"

    # fs should be enabled=True, max_items=42 (from kb.local.yml in fixture)
    assert sources["fs"]["enabled"] is True
    assert sources["fs"]["max_items"] == 42

    # env_overrides: empty when no KB_SOURCES__* vars are set
    assert parsed["env_overrides"] == [], (
        f"no KB_SOURCES__* vars set → env_overrides must be empty, "
        f"got {parsed['env_overrides']!r}"
    )


def test_get_channels_admin_credential_value_absent(channels_web, monkeypatch):
    """When YMAIL_APP_PASSWORD is set, credential_set=True but the value
    never appears in the response (FR-004)."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web
    monkeypatch.setenv("YMAIL_APP_PASSWORD", YMAIL_SECRET)

    code, parsed, raw = _http_get(
        host, port, "/api/config/channels",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:300]!r}"
    assert parsed["sources"]["yahoo"]["credential_set"] is True
    # The credential value must NOT appear anywhere in the response body.
    assert YMAIL_SECRET not in json.dumps(parsed), (
        "credential value leaked into response (FR-004 violation)"
    )


def test_get_channels_non_admin_403(channels_web):
    """GET as reader → 403 permission_denied."""
    _app, _db, host, port, _at, reader_token, _cd = channels_web

    code, parsed, raw = _http_get(
        host, port, "/api/config/channels",
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert code == 403, f"expected 403, got {code}: {raw[:300]!r}"
    assert parsed is not None
    assert parsed.get("error") in ("permission_denied", "forbidden", "403"), (
        f"403 body must carry an error code, got {parsed!r}"
    )


def test_get_channels_env_overrides_populated(channels_web, monkeypatch):
    """When KB_SOURCES__FS__MAX_ITEMS is set, env_overrides lists it."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web
    monkeypatch.setenv("KB_SOURCES__FS__MAX_ITEMS", "7")

    code, parsed, raw = _http_get(
        host, port, "/api/config/channels",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:300]!r}"
    assert "KB_SOURCES__FS__MAX_ITEMS" in parsed["env_overrides"], (
        f"env_overrides must list the shadowing var, got {parsed['env_overrides']!r}"
    )
    # The effective value must reflect the env layer (env wins).
    assert parsed["sources"]["fs"]["max_items"] == 7


# =============================================================================
# POST /api/config/channels
# =============================================================================

def test_post_channels_admin_200_happy_path(channels_web):
    """POST as admin → 200 post-write masked view; kb.local.yml updated;
    kb.yml untouched."""
    _app, _db, host, port, admin_token, _rt, config_dir = channels_web

    body = {"fs": {"enabled": True, "max_items": 50, "timeout_s": 90}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:400]!r}"
    assert parsed is not None, f"body must be JSON: {raw[:400]!r}"
    assert "sources" in parsed, f"post-write view must contain 'sources'"

    sources = parsed["sources"]
    assert sources["fs"]["enabled"] is True
    assert sources["fs"]["max_items"] == 50
    assert sources["fs"]["timeout_s"] == 90

    # On-disk kb.local.yml reflects the write.
    on_disk = yaml.safe_load(
        (config_dir / "kb.local.yml").read_text(encoding="utf-8"))
    assert on_disk["sources"]["fs"]["max_items"] == 50
    assert on_disk["sources"]["fs"]["timeout_s"] == 90
    # Unrelated keys preserved.
    assert on_disk["chunking"]["max_chars"] == 800

    # kb.yml untouched (still has the original llm.model).
    kb_yml = yaml.safe_load(
        (config_dir / "kb.yml").read_text(encoding="utf-8"))
    assert kb_yml["llm"]["model"] == "test-model"
    assert "sources" not in kb_yml, "kb.yml must not gain a sources block"


def test_post_channels_admin_200_multi_source(channels_web):
    """POST multiple sources in one call → 200; both updated on disk."""
    _app, _db, host, port, admin_token, _rt, config_dir = channels_web

    body = {"hermes": {"enabled": True}, "pi": {"max_items": 10}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:400]!r}"
    on_disk = yaml.safe_load(
        (config_dir / "kb.local.yml").read_text(encoding="utf-8"))
    assert on_disk["sources"]["hermes"]["enabled"] is True
    assert on_disk["sources"]["pi"]["max_items"] == 10


def test_post_channels_unknown_source_404(channels_web):
    """POST with an unknown source name → 404 naming it."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web

    body = {"nosuchsource": {"enabled": True}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 404, f"expected 404, got {code}: {raw[:300]!r}"
    assert parsed is not None
    msg = json.dumps(parsed).lower()
    assert "nosuchsource" in msg or "unknown" in msg, (
        f"404 body must name the unknown source, got {parsed!r}"
    )
    # No write performed: kb.local.yml unchanged (fs still enabled=True,
    # max_items still 42 from fixture).
    on_disk = yaml.safe_load(
        _cd.joinpath("kb.local.yml").read_text(encoding="utf-8"))
    assert on_disk["sources"]["fs"]["max_items"] == 42


def test_post_channels_custom_source_rejected_404(channels_web):
    """Carry-forward ruling: the web API does NOT register custom channels.
    A source not in the GET view (e.g. 'mytool' with entrypoint) → 404."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web

    # 'mytool' is not a built-in and not registered in kb.local.yml/kb.yml,
    # so it is not in the channel view → 404 even with an entrypoint key.
    # Also: unknown knobs (the CLI `channels add` surface) are 404 on the
    # web surface — it only exposes enabled / max_items / timeout_s.
    body = {"mytool": {"entrypoint": "digital_twins.sources.fs:factory",
                       "enabled": True}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 404, (
        f"custom registration via web API must be 404, got {code}: "
        f"{raw[:300]!r}"
    )


def test_post_channels_unknown_knob_404(channels_web):
    """POST with a knob outside the web surface (entrypoint / credential /
    prefix — the CLI `channels add` surface) → 404 naming the knob."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web

    body = {"fs": {"entrypoint": "digital_twins.sources.fs:factory"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 404, f"expected 404, got {code}: {raw[:300]!r}"
    assert parsed is not None
    msg = json.dumps(parsed).lower()
    assert "entrypoint" in msg, f"404 body must name the unknown knob: {parsed!r}"


def test_post_channels_out_of_range_422(channels_web):
    """POST with a negative value → 422 naming the knob."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web

    body = {"fs": {"max_items": -1}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 422, f"expected 422, got {code}: {raw[:300]!r}"
    assert parsed is not None
    msg = json.dumps(parsed).lower()
    assert "max_items" in msg, f"422 body must name the knob: {parsed!r}"


def test_post_channels_bad_value_422(channels_web):
    """POST with a type-invalid value → 422."""
    _app, _db, host, port, admin_token, _rt, _cd = channels_web

    # max_items must be int; sending a non-numeric string → 422.
    body = {"fs": {"max_items": "not-a-number"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 422, f"expected 422, got {code}: {raw[:300]!r}"
    assert parsed is not None
    msg = json.dumps(parsed).lower()
    assert "schema" in msg or "violation" in msg or "invalid" in msg or "type" in msg, (
        f"422 body must reference the validation failure, got {parsed!r}"
    )


def test_post_channels_non_admin_403(channels_web):
    """POST as reader → 403."""
    _app, _db, host, port, _at, reader_token, _cd = channels_web

    body = {"fs": {"enabled": False}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/channels", body,
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert code == 403, f"expected 403, got {code}: {raw[:300]!r}"
    assert parsed is not None
    assert parsed.get("error") in ("permission_denied", "forbidden", "403"), (
        f"403 body must carry an error code, got {parsed!r}"
    )
