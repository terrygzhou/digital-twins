"""T019 [006-web-app] RED: static UI pages + assets-present + fetch-target tests.

Integration tests for the 006 WebApp static UI surface (FR-015, SC-006,
NFR-16).  The static assets (``index.html`` / ``style.css``) do not exist
until T020, so tests 1-3 FAIL with 404 (the scaffold's clean not-found).
Tests 4-5 exercise the ``/api/*`` endpoints that already work.

Harness pattern mirrors T003's ``tests/unit/test_web_app_kb.py``:
migrated v3 state DB in ``tmp_path``, ``build_web_app`` on
``127.0.0.1:0``, socket-connect readiness, ``http.client`` for all
requests.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time

import pytest

from digital_twins.accounts import create_account
from digital_twins.state import db as state_db


# --- constants ----------------------------------------------------------------

#: The exact set of /api/* paths the UI must fetch (contracts/web-api.md,
#: SC-006: "drives them through /api/* only, no parallel client-side logic").
EXPECTED_FETCH_TARGETS = frozenset({
    "/api/auth/signup",
    "/api/auth/signin",
    "/api/auth/signout",
    "/api/me",
    "/api/kb/points",
    "/api/kb/search",
    "/api/ingest/run",
    "/api/audit/recent",
    "/api/kb/chat",
})

_READY_TIMEOUT_S = 5.0


# --- helpers ------------------------------------------------------------------


def _make_db(tmp_path):
    """Migrated v3 state DB (accounts / sessions / audit_runs / highwater)."""
    conn = state_db.connect(tmp_path)
    return conn


def _wait_server_ready(server, timeout=5.0):
    """Poll the listening socket until it accepts connections."""
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


def _extract_fetch_targets(html: str) -> set[str]:
    """Extract the first-argument string of every ``fetch('...')`` /
    ``fetch("...")`` call in the HTML.

    Only literal string targets are collected (dynamic / constructed URLs
    are out of scope for the "no parallel client-side logic" check).  The
    regex matches ``fetch('`` or ``fetch("`` followed by a path up to the
    closing quote, capturing the path.
    """
    return set(re.findall(r"""fetch\(\s*['"]([^'"]+)['"]""", html))


# --- fixture ------------------------------------------------------------------


@pytest.fixture
def web_app(tmp_path):
    """Build + serve the 006 WebApp on 127.0.0.1:0 with a v3 state DB.

    Yields ``(app, db, host, port)``.  No pre-created account — the
    e2e tests drive signup/signin through the API.
    """
    from digital_twins.web.app import build_web_app, serve

    db = _make_db(tmp_path)
    try:
        cfg = {"state_dir": str(tmp_path), "sources": {}}
        app = build_web_app(db, cfg, host="127.0.0.1", port=0)
        serve(app)

        host, port = app.server_address[:2]
        _wait_server_ready(app)

        yield app, db, host, port
    finally:
        app.shutdown()
        app.server_close()
        db.close()


# =============================================================================
# Test 1: GET / serves index.html
# =============================================================================


def test_get_root_serves_index_html(web_app):
    """GET / → 200, Content-Type text/html, body has sign-in, sign-up,
    and KB panel elements (point count, search field, trigger-ingest,
    last-audit-row).

    RED (before T020): the asset does not exist yet → clean JSON 404 with
    Content-Type text/html.  The body assertion (forms + KB panel) fails
    because the body is a 404 error, not a real page.
    """
    _app, _db, host, port = web_app
    code, parsed, raw, content_type = _http_get_full(host, port, "/")
    body = raw.decode("utf-8", errors="replace")

    assert code == 200, (
        f"GET / expected 200 (index.html), got {code}: {body[:300]!r}"
    )
    assert content_type.startswith("text/html"), (
        f"GET / Content-Type must be text/html, got {content_type!r}"
    )
    # FR-015: the page must contain a sign-in form, a sign-up form,
    # and a KB panel (point count display, search-query field,
    # trigger-ingestion control, last-audit-row display).
    lowered = body.lower()
    assert "sign in" in lowered or "sign-in" in lowered, (
        "index.html must contain a sign-in form"
    )
    assert "sign up" in lowered or "sign-up" in lowered, (
        "index.html must contain a sign-up form"
    )
    # KB panel elements
    assert "point" in lowered, (
        "index.html must display a point count"
    )
    assert "search" in lowered, (
        "index.html must have a search-query field"
    )
    assert "ingest" in lowered or "trigger" in lowered, (
        "index.html must have a trigger-ingestion control"
    )
    assert "audit" in lowered, (
        "index.html must show a last-audit-row display"
    )


# =============================================================================
# Test 2: GET /static/style.css → 200 text/css
# =============================================================================


def test_get_static_style_css_200(web_app):
    """GET /static/style.css → 200, Content-Type text/css.

    RED (before T020): the asset does not exist yet → clean JSON 404.
    The test asserts 200 + text/css, so it fails on the 404.
    """
    _app, _db, host, port = web_app
    code, parsed, raw, content_type = _http_get_full(
        host, port, "/static/style.css")
    assert code == 200, (
        f"GET /static/style.css expected 200, got {code}: "
        f"{raw[:200]!r}"
    )
    assert content_type.startswith("text/css"), (
        f"GET /static/style.css Content-Type must be text/css, "
        f"got {content_type!r}"
    )


# =============================================================================
# Test 3: index.html fetch targets are exactly the 9 /api/* paths
# =============================================================================


def test_index_html_fetch_targets_are_api_paths(web_app):
    """The index.html's ``fetch()`` calls target EXACTLY the 9 /api/*
    paths from contracts/web-api.md — no others, no parallel logic.

    RED (before T020): GET / returns a 404 JSON error (no HTML), so
    there are no ``fetch()`` calls to extract.  The extracted set is
    empty, which does not match the expected 9 paths → test fails.
    """
    _app, _db, host, port = web_app
    code, parsed, raw, _ct = _http_get_full(host, port, "/")
    assert code == 200, (
        f"GET / expected 200 (index.html) to extract fetch targets, "
        f"got {code}: {raw[:200]!r}"
    )
    html = raw.decode("utf-8", errors="replace")
    targets = _extract_fetch_targets(html)
    assert targets == EXPECTED_FETCH_TARGETS, (
        f"index.html fetch() targets must be EXACTLY the 9 /api/* paths.\n"
        f"Expected: {sorted(EXPECTED_FETCH_TARGETS)}\n"
        f"Got:      {sorted(targets)}\n"
        f"Missing:  {sorted(EXPECTED_FETCH_TARGETS - targets)}\n"
        f"Extra:    {sorted(targets - EXPECTED_FETCH_TARGETS)}"
    )


# =============================================================================
# Test 4: e2e signup → signin → me
# =============================================================================


def test_e2e_signup_signin_me(web_app):
    """Against the live WebApp: POST /api/auth/signup (first account,
    admin) → 200; POST /api/auth/signin → 200 with session_token;
    GET /api/me with the token → 200 {email, role, point_count}.

    These API endpoints already work (T004–T006), so this test PASSES
    even before T020 lands the static assets.
    """
    _app, _db, host, port = web_app

    # 1. Signup — first account gets admin role.
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "admin@example.com", "password": "admin-pw-123"},
    )
    assert code == 200, (
        f"POST /api/auth/signup expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"signup body must be JSON: {raw[:300]!r}"
    assert parsed.get("email") == "admin@example.com", (
        f"signup body email must echo the input, got {parsed!r}"
    )
    assert parsed.get("role") == "admin", (
        f"first account must be admin, got role={parsed.get('role')!r}"
    )
    assert parsed.get("created") is True, (
        f"signup body must have created=true: {parsed!r}"
    )

    # 2. Signin — returns a session token.
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "admin@example.com", "password": "admin-pw-123"},
    )
    assert code == 200, (
        f"POST /api/auth/signin expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"signin body must be JSON: {raw[:300]!r}"
    assert "session_token" in parsed, (
        f"signin body must contain session_token: {parsed!r}"
    )
    session_token = parsed["session_token"]
    assert isinstance(session_token, str) and session_token, (
        f"session_token must be a non-empty string: {parsed!r}"
    )

    # 3. /api/me with the Bearer token → 200 {email, role, point_count}.
    code, parsed, raw, _ct = _http_get_full(
        host, port, "/api/me",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert code == 200, (
        f"GET /api/me expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"/api/me body must be JSON: {raw[:300]!r}"
    assert parsed.get("email") == "admin@example.com", (
        f"/api/me email must be the caller's, got {parsed!r}"
    )
    assert parsed.get("role") == "admin", (
        f"/api/me role must be admin, got {parsed!r}"
    )
    assert "point_count" in parsed, (
        f"/api/me body must contain point_count: {parsed!r}"
    )
    assert isinstance(parsed["point_count"], int), (
        f"point_count must be an int: {parsed!r}"
    )


# =============================================================================
# Test 5: e2e KB points + search (Qdrant unavailable is acceptable)
# =============================================================================


def test_e2e_kb_points_and_search(web_app):
    """With a session token: GET /api/kb/points → 200 (count=0 on empty)
    OR 502/503 (Qdrant unavailable).  POST /api/kb/search {query:"test"}
    → 200 or 502/503 (Qdrant unavailable is acceptable).

    These API endpoints already work (T008/T010), so this test PASSES
    even before T020 lands the static assets.  In this test environment
    Qdrant is not configured, so the handlers return 503 with a clean
    error — the test accepts both 200 (if Qdrant is available) and
    502/503 (Qdrant unavailable).
    """
    _app, _db, host, port = web_app

    # Get a session token (reuse the same flow as test 4).
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "kb-test@example.com", "password": "kb-pw-123"},
    )
    assert code in (200, 409), (
        f"signup expected 200/409, got {code}: {raw[:200]!r}"
    )
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "kb-test@example.com", "password": "kb-pw-123"},
    )
    assert code == 200, (
        f"signin expected 200, got {code}: {raw[:200]!r}"
    )
    session_token = parsed["session_token"]
    auth = {"Authorization": f"Bearer {session_token}"}

    # GET /api/kb/points → 200 (count=0 on empty) or 502/503 (Qdrant down).
    code, parsed, raw, _ct = _http_get_full(
        host, port, "/api/kb/points", headers=auth)
    assert code in (200, 502, 503), (
        f"GET /api/kb/points expected 200 or 502/503, got {code}: "
        f"{raw[:300]!r}"
    )
    if code == 200:
        assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
        assert parsed.get("count") == 0, (
            f"empty collection must have count=0, got {parsed.get('count')}"
        )
    else:
        assert parsed is not None and "error" in parsed, (
            f"502/503 body must be JSON with 'error': {raw[:300]!r}"
        )
        err = str(parsed["error"]).lower()
        assert "qdrant" in err, (
            f"error must name Qdrant: {parsed['error']!r}"
        )

    # POST /api/kb/search {query:"test"} → 200 or 502/503.
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": "test"},
        headers=auth,
    )
    assert code in (200, 502, 503), (
        f"POST /api/kb/search expected 200 or 502/503, got {code}: "
        f"{raw[:300]!r}"
    )
    if code == 200:
        assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
        assert "results" in parsed, (
            f"search body must have 'results': {parsed!r}"
        )
    else:
        assert parsed is not None and "error" in parsed, (
            f"502/503 body must be JSON with 'error': {raw[:300]!r}"
        )
        err = str(parsed["error"]).lower()
        assert "qdrant" in err, (
            f"error must name Qdrant: {parsed['error']!r}"
        )
