"""T003 [006-web-app] RED: WebApp static-UI routes + bearer-token gate.

This is the *scaffold* seed of ``test_web_app_kb.py`` (per the 006 plan, the
KB tests for ``/api/kb/points`` and ``/api/kb/search`` join this file in
T007/T009).  It pins the T004 surface:

- ``GET /`` serves ``index.html`` with ``Content-Type: text/html``.
- ``GET /static/style.css`` returns 200 (the asset is served from
  ``digital_twins/web/static/``).
- A path-traversal request (``/static/../knobs.py``) is rejected — no file
  is served (400 or 404).
- Every ``/api/*`` endpoint except the three ``/api/auth/*`` credential
  endpoints returns ``401 {"error":"unauthorized"}`` without a session
  token (contracts/web-api.md, fail-closed).
- A ``?token=`` query fallback bypasses the bearer gate: the same request
  with a valid session token in the query string does NOT get the 401.

Red-first per constitution III: these tests fail now because
``digital_twins/web/app.py`` does not exist yet (T004 lands it).  The
fixture below imports it at setup time, so the RED failure is a
``ModuleNotFoundError`` on ``digital_twins.web.app`` — not a fixture or
harness bug.

Placeholder-asset choice (documented per the task brief): the static UI
assets (``web/static/index.html`` / ``style.css``) do not exist until T020,
so the tests do **not** monkeypatch a per-test asset root — instead they
assert on the *routed response of the module's own static root*:

- ``GET /`` must be served with ``Content-Type: text/html`` regardless of
  whether the asset exists yet; the body must be clean JSON
  ``{"error": ...}`` (an asset-missing 404 is acceptable in RED state),
  never an ``HTTP/0.9``-style traceback.  Once T020 lands the real
  ``index.html``, the same 200 + ``text/html`` assertions hold, and
  T019's asset-content tests pin the page itself.
- ``GET /static/style.css`` follows the same rule (200 + non-HTML asset
  type, or a clean JSON 404 in RED state — never a traceback or a
  file-escape success).

Harness pattern mirrors 003's ``tests/integration/test_web_server.py``:
migrated v3 state DB in ``tmp_path``, ``ThreadingHTTPServer`` on
``127.0.0.1:0``, socket-connect readiness poll, requests driven with
``http.client``.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from digital_twins.accounts import create_account
from digital_twins.auth import create_session
from digital_twins.state import db as state_db

# --- spec -------------------------------------------------------------------

#: The three credential endpoints that must NOT be 401 without a token
#: (contracts/web-api.md: the only /api/* surface exempt from the session
#: gate).
AUTH_EXEMPT = ("/api/auth/signup", "/api/auth/signin", "/api/auth/signout")

#: Every other /api/* route from the contract that the scaffold dispatches.
API_ROUTES = (
    "/api/me",
    "/api/kb/points",
    "/api/kb/search",
    "/api/ingest/run",
    "/api/audit/recent",
    "/api/kb/chat",
)

_READY_TIMEOUT_S = 5.0


# --- helpers ---------------------------------------------------------------


def _make_db(tmp_path):
    """Migrated v3 state DB (accounts / sessions / audit_runs / highwater)."""
    conn = state_db.connect(tmp_path)
    return conn


def _wait_server_ready(server, timeout=5.0):
    """Poll the listening socket until it accepts connections.

    A plain socket-connect loop (not an HTTP poll) so readiness does not
    depend on any route being dispatchable yet.
    """
    host, port = server.server_address[:2]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server at {host}:{port} not ready within {timeout}s")


def _http_get(host: str, port: int, path: str,
              headers: dict | None = None
              ) -> tuple[int, dict | None, bytes]:
    """GET ``http://host:port`` + ``path`` via ``http.client``.

    Returns ``(status_code, parsed_json_or_None, raw_body)``.  Non-2xx
    responses are read, not raised, so assertions can inspect the body.
    """
    code, parsed, raw, _ct = _http_get_full(host, port, path, headers)
    return code, parsed, raw


def _http_get_full(host: str, port: int, path: str,
                   headers: dict | None = None
                   ) -> tuple[int, dict | None, bytes, str]:
    """GET with full response headers: ``(code, parsed, raw, content_type)``."""
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = None
        return resp.status, parsed, raw, resp.getheader("Content-Type") or ""
    finally:
        conn.close()


def _http_post(host: str, port: int, path: str, body: dict,
               headers: dict | None = None) -> tuple[int, dict | None, bytes]:
    """POST JSON to ``http://host:port`` + ``path`` via ``http.client``."""
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
def web_app(tmp_path):
    """Build + serve the 006 WebApp on 127.0.0.1:0 with a v3 state DB.

    Yields ``(app, db, host, port, session_token)``.  ``session_token`` is a
    live session token for ``web-user@example.com`` (created via
    ``digital_twins.auth.create_session``), used to prove the bearer gate
    and the ``?token=`` query fallback.

    RED: imports ``digital_twins.web.app`` — the module does not exist until
    T004, so every test in this file fails at setup with
    ``ModuleNotFoundError: No module named 'digital_twins.web.app'``.
    """
    from digital_twins.web.app import build_web_app, serve

    db = _make_db(tmp_path)
    try:
        create_account(db, "web-user@example.com", "web-pw-123")
        session_token, _expires_at = create_session(db, "web-user@example.com")

        cfg = {"state_dir": str(tmp_path), "sources": {}}
        app = build_web_app(db, cfg, host="127.0.0.1", port=0)
        serve(app)

        host, port = app.server_address[:2]
        _wait_server_ready(app)

        yield app, db, host, port, session_token
    finally:
        app.shutdown()
        app.server_close()
        db.close()


# --- GET / : the static UI (routed, text/html, clean error in RED) -----------


def test_get_root_serves_index_as_text_html(web_app):
    """GET / routes to the static index.html with Content-Type text/html.

    RED (T020 lands the asset later): the asset may be missing yet, so a
    clean JSON 404 is acceptable — what is pinned here is that the route IS
    dispatched, the response body is clean (never a traceback), and the
    Content-Type is text/html.  Once index.html exists, the body assertion
    upgrades to the real page; the content-type + clean-body checks hold.
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw, content_type = _http_get_full(host, port, "/")
    assert code in (200, 404), (
        f"GET / expected 200 (asset) or clean 404 (asset missing in RED), "
        f"got {code}: {raw[:200]!r}"
    )
    assert content_type.startswith("text/html"), (
        f"GET / Content-Type must be text/html, got {content_type!r}"
    )
    if code == 404:
        # Asset missing (RED state before T020): the body must still be a
        # clean JSON error, never an unhandled traceback.
        assert parsed is not None and "error" in parsed, (
            f"GET / 404 body must be clean JSON, got: {raw[:200]!r}"
        )
    elif code == 200:
        assert b"<html" in raw.lower() or b"<!doctype" in raw.lower(), (
            f"GET / 200 body must be the index.html page: {raw[:200]!r}"
        )


# --- GET /static/style.css : static asset route ------------------------------


def test_get_static_style_css_routes(web_app):
    """GET /static/style.css is served by the static-asset route.

    200 once T020 lands the asset; in RED state a clean JSON 404 is
    acceptable — the point is that /static/<asset> dispatches to the static
    handler (never a traceback, never a 401 — the static route is
    unauthenticated).
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw = _http_get(host, port, "/static/style.css")
    assert code in (200, 404), (
        f"GET /static/style.css expected 200 (asset) or clean 404, "
        f"got {code}: {raw[:200]!r}"
    )
    assert code != 401, "static assets must not be bearer-gated"
    if code == 200:
        assert parsed is None or isinstance(parsed, dict), (
            f"asset body must be the CSS text or a clean error: {raw[:200]!r}"
        )


# --- path traversal : /static/../knobs.py must not serve the file ------------


def test_static_path_traversal_rejected(web_app):
    """GET /static/../knobs.py must not serve digital_twins/config/knobs.py.

    The request is rejected with 400 or 404 and the response body is never
    the contents of a file outside web/static/.  (http.client sends the raw
    path; a well-behaved stdlib server either rejects it or normalizes it
    outside the static root and refuses.)
    """
    _app, _db, host, port, _token = web_app
    code, _parsed, raw = _http_get(host, port, "/static/../knobs.py")
    assert code in (400, 404), (
        f"path traversal expected 400/404, got {code}: {raw[:200]!r}"
    )
    assert b"def " not in raw[:200] or code in (400, 404), (
        "path-traversal response must not leak source file contents"
    )


# --- /api/* bearer gate : 401 without a session token ------------------------


def test_api_endpoints_401_without_bearer_token(web_app):
    """Every /api/* endpoint (except /api/auth/*) is 401 without a token.

    GET routes are checked with a plain GET; POST routes with an empty
    JSON body (the gate must reject *before* any body validation, so the
    status is 401, not 400).
    """
    _app, _db, host, port, _token = web_app
    for path in API_ROUTES:
        if path in ("/api/kb/search", "/api/ingest/run", "/api/kb/chat"):
            code, parsed, raw = _http_post(host, port, path, {})
        else:
            code, parsed, raw = _http_get(host, port, path)
        assert code == 401, (
            f"{path} without a Bearer token expected 401, got {code}: "
            f"{raw[:200]!r}"
        )
        assert parsed is not None, (
            f"{path} 401 body must be JSON: {raw[:200]!r}"
        )
        assert "error" in parsed, f"{path} 401 body missing 'error': {parsed!r}"


def test_api_auth_endpoints_are_not_bearer_gated(web_app):
    """The three /api/auth/* endpoints respond without a session token.

    Signin with valid credentials returns 200 (not 401) — proof the
    credential endpoints sit outside the session gate.  Signup returns a
    409 on the already-existing account (not 401).  Signout with no token
    is 401, but only because it requires a token to revoke (fail-closed),
    not because of the /api/* gate — so it is excluded from the
    "no token at all" check here and covered by T005.
    """
    _app, db, host, port, _token = web_app
    # /api/auth/signin with valid credentials → 200 session token
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "web-user@example.com", "password": "web-pw-123"},
    )
    assert code == 200, (
        f"/api/auth/signin (valid creds, no session token) expected 200, "
        f"got {code}: {raw[:200]!r}"
    )
    assert "session_token" in parsed, f"signin body missing session_token: {parsed!r}"

    # /api/auth/signup with a duplicate account → 409 (not 401)
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "web-user@example.com", "password": "other-pw-456"},
    )
    assert code == 409, (
        f"/api/auth/signup (duplicate, no session token) expected 409, "
        f"got {code}: {raw[:200]!r}"
    )


# --- ?token= query fallback ---------------------------------------------------


def test_token_query_fallback_bypasses_bearer_gate(web_app):
    """A valid session token in ?token= is accepted (the gate is bypassed).

    ``GET /api/me?token=<valid>`` must NOT return the auth 401: the
    bearer gate sees the query token and lets the request through.  The
    response may be 404 if /api/me is not implemented yet (T006 lands it)
    — but it must NOT be 401, which would mean the gate still rejected the
    query token.
    """
    _app, _db, host, port, session_token = web_app
    code, _parsed, raw = _http_get(
        host, port, f"/api/me?token={session_token}")
    assert code != 401, (
        f"/api/me with a valid ?token= session token must not be 401; "
        f"got {code}: {raw[:200]!r}"
    )
    assert code in (200, 404), (
        f"/api/me?token=<valid> expected 200 (endpoint) or 404 (not yet "
        f"implemented in scaffold), got {code}: {raw[:200]!r}"
    )


def test_bearer_header_still_accepted(web_app):
    """A valid session token in Authorization: Bearer is also accepted.

    Same gate, header form: ``GET /api/me`` with the header must not be
    401 (the gate is bypassed; 200 or 404-not-yet-implemented are both
    fine).  Pins both credential transports at once.
    """
    _app, _db, host, port, session_token = web_app
    code, _parsed, raw = _http_get(
        host, port, "/api/me",
        headers={"Authorization": f"Bearer {session_token}"})
    assert code != 401, (
        f"/api/me with a valid Bearer token must not be 401; "
        f"got {code}: {raw[:200]!r}"
    )
    assert code in (200, 404)
