"""008/US2 (T023 + T024, RED-first): web admin UI — service config API.

Contract: ``specs/008-service-hosting/contracts/web-config-api.md``.

Both routes are admin-gated (``accounts.get_role(db, caller_email)`` →
``admin``; non-admin → 403; missing/invalid bearer → 401).  Credential
values are **never returned** — only set/not-set flags (FR-004).

T023 (GET) assertions:
* admin → 200 with the masked view (effective merged values,
  ``*_set`` booleans, ``env_overrides`` list populated when an env var
  shadows a value).
* non-admin → 403.
* missing bearer → 401.

T024 (POST) assertions:
* partial update → 200 post-write view (submitted key not echoed,
  ``*_set: true``).
* persists to ``kb.local.yml`` via ``merge_write`` (unrelated keys
  preserved).
* 404 unknown service.
* 422 schema-invalid value.
* 409 unparseable existing YAML (no write).  **GREEN-on-first-run
  expected** (ruling T024): the 007/008 scaffolding already returns 409
  for this route shape.
* non-admin → 403.
* request bodies containing keys are **not logged** (masked-view log
  only).  RED: the log line for a POST /api/config/services carrying
  ``api_key`` must not contain the key value.

Harness pattern mirrors ``tests/integration/test_web_app.py``:
migrated v3 state DB in ``tmp_path``, ``build_web_app`` on
``127.0.0.1:0``, socket-connect readiness, ``http.client`` for all
requests.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import time
from pathlib import Path

import pytest
import yaml

from digital_twins.accounts import create_account
from digital_twins.auth import create_session
from digital_twins.state import db as state_db
from digital_twins.web.app import build_web_app, serve

# --- obviously-fake credential values (FR-004 safe) --------------------------

QDRANT_KEY = "sk-test-fake-008-us2-qdrant"
NEO4J_PASSWORD = "sk-test-fake-008-us2-neo4j"
LLM_KEY = "sk-test-fake-008-us2-llm"
EMB_KEY = "sk-test-fake-008-us2-embedding"

_READY_TIMEOUT_S = 5.0


# --- helpers ------------------------------------------------------------------


def _make_db(tmp_path):
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
    raise RuntimeError(
        f"server at {host}:{port} not ready within {timeout}s")


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


def _signup_signin(host, port, email, password="pw-123", role=None):
    """POST /api/auth/signup (first account = admin) or create_account +
    sign-in.  Returns the session token."""
    if role is None:
        # first account via the API
        code, parsed, raw = _http_post(
            host, port, "/api/auth/signup",
            {"email": email, "password": password},
        )
        assert code == 200, f"signup failed: {code} {raw[:200]!r}"
    else:
        # non-first accounts: create via accounts module, then sign-in
        from digital_twins.accounts import create_account as _ca
        # We need a db handle; the fixture yields one, so this helper is
        # used *after* the fixture has created the first admin.
        raise RuntimeError(
            "use the admin + extra-account pattern in the fixture instead")
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": email, "password": password},
    )
    assert code == 200, f"signin failed: {code} {raw[:200]!r}"
    return parsed["session_token"]


# --- fixture ------------------------------------------------------------------


@pytest.fixture
def web_config_app(tmp_path, monkeypatch):
    """WebApp with:

    * a v3 state DB in ``tmp_path``,
    * an admin account (``admin@example.com``) + a reader account
      (``reader@example.com``),
    * a ``kb.local.yml`` in ``tmp_path/config`` with pre-existing
      credentials (to verify the masked view + merge_write preservation),
    * the WebApp served on 127.0.0.1:0.

    Yields ``(app, db, host, port, admin_token, reader_token,
    config_dir)``.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Pre-existing kb.local.yml with credentials the API must NOT echo.
    local_yaml = {
        "qdrant": {"url": "http://qdrant:6333", "api_key": QDRANT_KEY},
        "neo4j": {"url": "bolt://neo4j:7687", "user": "neo4j",
                   "password": NEO4J_PASSWORD},
        "llm": {"endpoint": "http://llm:8000/v1", "api_key": LLM_KEY},
        "embedding": {"endpoint": "", "api_key": ""},
        # Unrelated key that must survive a POST merge_write.
        "chunking": {"max_chars": 800, "overlap": 100},
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(local_yaml, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))

    db = _make_db(tmp_path)
    create_account(db, "admin@example.com", "admin-pw-123", role="admin")
    create_account(db, "reader@example.com", "reader-pw-123", role="reader")
    db.commit()

    # create_session returns (plaintext_token, expires_at).
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
# T023 — GET /api/config/services
# =============================================================================


def test_get_config_services_admin_200_masked_view(web_config_app):
    """GET /api/config/services as admin → 200 with the masked view per
    the contract: effective merged values, ``*_set`` booleans, and
    ``env_overrides`` list.

    RED: the route does not exist yet → the current app returns 404.
    """
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app

    code, parsed, raw = _http_get(
        host, port, "/api/config/services",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, (
        f"GET /api/config/services (admin) expected 200, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"

    services = parsed.get("services")
    assert services is not None, (
        f"response must contain 'services', got keys={list(parsed.keys())!r}"
    )

    # --- qdrant: url = effective merged value; api_key_set = True --------
    q = services.get("qdrant")
    assert q is not None, f"services.qdrant missing: {services.keys()}"
    assert q.get("url") == "http://qdrant:6333", (
        f"qdrant.url must be the effective merged value, got {q.get('url')!r}"
    )
    assert q.get("api_key_set") is True, (
        f"qdrant.api_key_set must be True (kb.local.yml has the key), "
        f"got {q.get('api_key_set')!r}"
    )
    # FR-004: the key value itself must NOT appear anywhere in the response.
    assert QDRANT_KEY not in json.dumps(parsed), (
        "qdrant.api_key value must not be returned (FR-004): only "
        "api_key_set bool"
    )

    # --- neo4j: url + user_set + password_set ----------------------------
    n = services.get("neo4j")
    assert n is not None, "services.neo4j missing"
    assert n.get("url") == "bolt://neo4j:7687"
    assert n.get("user_set") is True, "neo4j.user_set must be True"
    assert n.get("password_set") is True, "neo4j.password_set must be True"
    assert NEO4J_PASSWORD not in json.dumps(parsed), (
        "neo4j.password value must not be returned (FR-004)"
    )

    # --- llm: endpoint + api_key_set --------------------------------------
    l = services.get("llm")
    assert l is not None, "services.llm missing"
    assert l.get("url") == "http://llm:8000/v1"
    assert l.get("api_key_set") is True
    assert LLM_KEY not in json.dumps(parsed), (
        "llm.api_key value must not be returned (FR-004)"
    )

    # --- embedding: endpoint unset → url null/empty, api_key_set False ----
    e = services.get("embedding")
    assert e is not None, "services.embedding missing"
    # endpoint was set to "" in kb.local.yml → effective value is "" or None
    assert e.get("url") in (None, ""), (
        f"embedding.url must be null/empty when endpoint is unset, "
        f"got {e.get('url')!r}"
    )
    assert e.get("api_key_set") is False, (
        f"embedding.api_key_set must be False, got {e.get('api_key_set')!r}"
    )

    # --- env_overrides: empty when no KB_* env vars shadow values ---------
    assert "env_overrides" in parsed, (
        "response must contain 'env_overrides'"
    )
    assert parsed["env_overrides"] == [], (
        f"no KB_* env vars are set in this test, so env_overrides must "
        f"be empty, got {parsed['env_overrides']!r}"
    )


def test_get_config_services_env_overrides_populated(
        web_config_app, monkeypatch):
    """When a ``KB_*`` env var shadows a service value, ``env_overrides``
    must list the env var name.

    RED: the route does not exist yet.
    """
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app

    # Shadow qdrant.url via env (highest precedence).
    monkeypatch.setenv("KB_QDRANT__URL", "http://qdrant-env:6333")

    code, parsed, raw = _http_get(
        host, port, "/api/config/services",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:300]!r}"
    assert parsed is not None

    # env_overrides must name the shadowing var.
    overrides = parsed.get("env_overrides", [])
    assert "KB_QDRANT__URL" in overrides, (
        f"env_overrides must list KB_QDRANT__URL when it shadows "
        f"qdrant.url, got {overrides!r}"
    )

    # The effective value must reflect the env layer (env wins).
    q = parsed["services"]["qdrant"]
    assert q.get("url") == "http://qdrant-env:6333", (
        f"effective qdrant.url must come from the env layer, "
        f"got {q.get('url')!r}"
    )


def test_get_config_services_non_admin_403(web_config_app):
    """GET /api/config/services as a non-admin (reader) → 403.

    RED: the route does not exist yet → currently 404.
    """
    _app, _db, host, port, _at, reader_token, _cd = web_config_app

    code, parsed, raw = _http_get(
        host, port, "/api/config/services",
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert code == 403, (
        f"GET /api/config/services (non-admin) expected 403, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None
    assert parsed.get("error") in ("permission_denied", "forbidden", "403"), (
        f"403 body must carry an error code, got {parsed!r}"
    )


def test_get_config_services_missing_bearer_401(web_config_app):
    """GET /api/config/services without a bearer token → 401.

    This one may be GREEN-on-first-run if the 007/008 bearer gate already
    applies to every /api/* route.  Record in the report.
    """
    _app, _db, host, port, _at, _rt, _cd = web_config_app

    code, parsed, raw = _http_get(
        host, port, "/api/config/services",
    )
    assert code == 401, (
        f"GET /api/config/services (no bearer) expected 401, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None
    assert parsed.get("error") in ("unauthorized", "401"), (
        f"401 body must carry the 'unauthorized' error, got {parsed!r}"
    )


def test_get_config_services_invalid_bearer_401(web_config_app):
    """GET /api/config/services with a garbage bearer → 401."""
    _app, _db, host, port, _at, _rt, _cd = web_config_app

    code, parsed, raw = _http_get(
        host, port, "/api/config/services",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert code == 401, (
        f"GET /api/config/services (invalid bearer) expected 401, got "
        f"{code}: {raw[:300]!r}"
    )


# =============================================================================
# T024 — POST /api/config/services
# =============================================================================


def test_post_config_services_partial_update_200_masked(
        web_config_app):
    """POST /api/config/services with a partial update → 200 post-write
    view.  The submitted key is NOT echoed; ``*_set: true`` reflects the
    write.  Unrelated keys (chunking) are preserved.

    RED: the route does not exist yet.
    """
    _app, _db, host, port, admin_token, _rt, config_dir = web_config_app

    body = {
        "llm": {
            "endpoint": "http://llm-new:9000/v1",
            "api_key": "sk-test-fake-008-us2-llm-rotated",
        },
    }
    code, parsed, raw = _http_post(
        host, port, "/api/config/services", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, (
        f"POST /api/config/services expected 200, got {code}: {raw[:400]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:400]!r}"

    services = parsed.get("services")
    assert services is not None, (
        f"200 response must contain the post-write services view, "
        f"got keys={list(parsed.keys())!r}"
    )

    llm = services.get("llm")
    assert llm is not None, "services.llm missing in post-write view"
    assert llm.get("url") == "http://llm-new:9000/v1", (
        f"llm.url must reflect the post-write value, got {llm.get('url')!r}"
    )
    assert llm.get("api_key_set") is True, (
        f"llm.api_key_set must be True after the write, "
        f"got {llm.get('api_key_set')!r}"
    )
    # FR-004: the submitted key must NOT be echoed.
    assert "sk-test-fake-008-us2-llm-rotated" not in json.dumps(parsed), (
        "the submitted api_key must not be echoed in the response (FR-004)"
    )

    # Unrelated keys preserved: chunking still present with original values.
    chunking = parsed.get("chunking") or parsed.get("services", {}).get(
        "chunking")
    # The contract's GET shape only includes the 4 services; chunking may
    # or may not appear.  What we MUST verify is that the on-disk
    # kb.local.yml still has the chunking block.  (Asserted in the
    # persistence test below; here we just ensure the response didn't
    # lose the 4-service structure.)
    assert set(services.keys()) >= {"qdrant", "neo4j", "llm", "embedding"}, (
        f"post-write view must still include all 4 services, "
        f"got {set(services.keys())}"
    )


def test_post_config_services_persists_via_merge_write(web_config_app):
    """POST /api/config/services persists to kb.local.yml via
    ``config/local_io.merge_write``: the new value lands, and unrelated
    keys (chunking, the other services) are preserved.

    RED: the route does not exist yet → no write happens.
    """
    _app, _db, host, port, admin_token, _rt, config_dir = web_config_app

    body = {"qdrant": {"api_key": "sk-test-fake-008-us2-qdrant-rotated"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/services", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:300]!r}"

    # Read the on-disk kb.local.yml and assert the write landed + the
    # unrelated keys survived.
    local_file = config_dir / "kb.local.yml"
    assert local_file.is_file(), "kb.local.yml must exist after the POST"
    on_disk = yaml.safe_load(local_file.read_text(encoding="utf-8"))

    assert on_disk.get("qdrant", {}).get("api_key") == \
        "sk-test-fake-008-us2-qdrant-rotated", (
        f"qdrant.api_key must be the new value on disk, "
        f"got {on_disk.get('qdrant', {}).get('api_key')!r}"
    )
    # Unrelated key preserved (merge_write, not overwrite).
    assert on_disk.get("chunking", {}).get("max_chars") == 800, (
        f"chunking.max_chars must survive the merge_write, "
        f"got {on_disk.get('chunking')!r}"
    )
    assert on_disk.get("neo4j", {}).get("url") == "bolt://neo4j:7687", (
        f"neo4j.url must survive the merge_write, "
        f"got {on_disk.get('neo4j')!r}"
    )
    # The original qdrant.url is preserved (only api_key was updated).
    assert on_disk.get("qdrant", {}).get("url") == "http://qdrant:6333", (
        f"qdrant.url must be preserved, got {on_disk.get('qdrant')!r}"
    )


def test_post_config_services_unknown_service_404(web_config_app):
    """POST /api/config/services with an unknown service → 404
    ``unknown service "x"``.

    RED: the route does not exist yet.
    """
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app

    body = {"nosuchservice": {"url": "http://x"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/services", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 404, (
        f"POST unknown service expected 404, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None
    msg = json.dumps(parsed).lower()
    assert "unknown service" in msg or "nosuchservice" in msg, (
        f"404 body must name the unknown service, got {parsed!r}"
    )


def test_post_config_services_schema_invalid_422(web_config_app):
    """POST /api/config/services with a schema-invalid value → 422
    ``schema violation: <key>``.

    Example: ``qdrant.max_items: "not-a-number"`` (int knob, string value)
    or a value that fails the config-layer validator.

    RED: the route does not exist yet.
    """
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app

    # chunking.max_chars is an int knob; sending a non-numeric string must
    # fail the config-layer validation (same validator as load()).
    body = {"chunking": {"max_chars": "not-a-number"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/services", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 422, (
        f"POST schema-invalid value expected 422, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None
    msg = json.dumps(parsed).lower()
    assert "schema" in msg or "violation" in msg or "invalid" in msg, (
        f"422 body must reference the schema violation, got {parsed!r}"
    )


def test_post_config_services_unparseable_yaml_409(web_config_app):
    """POST /api/config/services when the existing kb.local.yml is
    unparseable YAML → 409, and **no write is performed**.

    **GREEN-on-first-run expected** (ruling T024): the 007/008 scaffolding
    already returns 409 for this route shape.  The *no-write* assertion is
    the meaningful part.
    """
    _app, _db, host, port, admin_token, _rt, config_dir = web_config_app

    # Corrupt the existing kb.local.yml with unparseable YAML.
    local_file = config_dir / "kb.local.yml"
    original_content = local_file.read_text(encoding="utf-8")
    local_file.write_text(
        "this is: [not: valid: yaml: {{{", encoding="utf-8")

    body = {"qdrant": {"url": "http://qdrant:6333"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/services", body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    try:
        assert code == 409, (
            f"POST with unparseable kb.local.yml expected 409, got {code}: "
            f"{raw[:300]!r}"
        )
        # No write performed: the file must still contain the corrupt
        # content (the merge_write refused to touch it).
        on_disk = local_file.read_text(encoding="utf-8")
        assert on_disk == "this is: [not: valid: yaml: {{{", (
            f"the corrupt kb.local.yml must be left intact on 409, "
            f"got {on_disk!r}"
        )
    finally:
        # Restore the original content for any later test in the same
        # fixture (though each test gets a fresh fixture instance).
        local_file.write_text(original_content, encoding="utf-8")


def test_post_config_services_non_admin_403(web_config_app):
    """POST /api/config/services as a non-admin (reader) → 403.

    RED: the route does not exist yet → currently 404.
    """
    _app, _db, host, port, _at, reader_token, _cd = web_config_app

    body = {"llm": {"endpoint": "http://llm:8000/v1"}}
    code, parsed, raw = _http_post(
        host, port, "/api/config/services", body,
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert code == 403, (
        f"POST /api/config/services (non-admin) expected 403, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None
    assert parsed.get("error") in ("permission_denied", "forbidden", "403"), (
        f"403 body must carry an error code, got {parsed!r}"
    )


def test_post_config_services_request_body_not_logged(web_config_app):
    """POST /api/config/services with an api_key in the body: the request
    body must NOT be logged (masked-view log only).  FR-004.

    RED: the route does not exist yet, so there is no log line at all —
    the assertion is vacuously true.  When the route lands, this must
    still hold: the log line (if any) must not contain the key value.
    """
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    root = logging.getLogger("digital_twins")
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    secret_key = "sk-test-fake-008-us2-logged"
    body = {"llm": {"api_key": secret_key}}
    try:
        code, _parsed, _raw = _http_post(
            host, port, "/api/config/services", body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        # 200 or 4xx/5xx are all acceptable for this assertion — the
        # point is the log hygiene, not the status code.
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    all_log_text = "\n".join(
        (r.getMessage() + " " + " ".join(map(repr, r.args))
         if isinstance(r.args, tuple) else r.getMessage())
        for r in records
    )
    assert secret_key not in all_log_text, (
        f"the request body containing llm.api_key was logged (FR-004): "
        f"{all_log_text[:500]!r}"
    )
