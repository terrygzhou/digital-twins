"""T005 [006-web-app] RED: /api/auth/* signup-signin-signout + /api/me tests.

Pins the T006 auth-handler surface on the 006 WebApp (contracts/web-api.md):

- ``POST /api/auth/signup`` — first-ever account → 200 ``role: "admin"`` +
  ``created: True``; a later account → ``role: "reader"``; duplicate email →
  409 ``{"error": "account already exists"}``; blank email or password → 400.
- ``POST /api/auth/signin`` — valid credentials → 200 with a non-empty
  ``session_token`` (str) + ``expires_at``; wrong password → 401 with NO
  ``session_token`` key; unknown email → 401.
- ``POST /api/auth/signout`` — Bearer token → 200 ``{"revoked": True}`` and
  the same token then 401s on ``/api/*``; no token → 401 (fail-closed).
- ``GET /api/me`` — valid Bearer token → 200 with ``email`` + ``role`` +
  ``point_count`` (0 on an empty collection; the handler must degrade to 0
  when Qdrant is unreachable); no token → 401; garbage Bearer token → 401.

Red-first per constitution III: the RED gate for these tests is
``/api/me`` — the T004 scaffold's ``_dispatch_rest`` returns a clean JSON
404 ``{"error": "not_found", "path": "/api/me"}`` for any unimplemented
gated route, and T006 lands the ``/api/me`` handler in
``digital_twins/web/app.py``.  (The T004 commit pulled the
``/api/auth/*`` handlers forward from the 003 surface, so the auth
endpoint tests already pass here — they are pinned, not gated.)  The
fixture and helpers below copy the T003 ``test_web_app_kb.py`` pattern
verbatim (self-contained; the auth file does not import from the kb
test module).
"""

from __future__ import annotations

import http.client
import json
import socket
import time

import pytest

from digital_twins.state import db as state_db


# --- helpers ---------------------------------------------------------------


def _make_db(tmp_path):
    """Migrated v3 state DB (accounts / sessions / audit_runs / highwater)."""
    return state_db.connect(tmp_path)


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

    Returns ``(status_code, parsed_json_or_None, raw_body)``.
    """
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


def _signin(host, port, email, password="pw12345"):
    """POST /api/auth/signin; return the parsed body (200 → token dict)."""
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": email, "password": password},
    )
    assert code == 200, (
        f"signin as {email!r} expected 200, got {code}: {raw[:200]!r}"
    )
    return parsed


# --- fixture ----------------------------------------------------------------


@pytest.fixture
def web_app(tmp_path):
    """Build + serve the 006 WebApp on 127.0.0.1:0 with a v3 state DB.

    Yields ``(app, db, host, port)``; the DB starts EMPTY (no accounts), so
    the first ``/api/auth/signup`` call is the first-ever account.
    """
    from digital_twins.web.app import build_web_app, serve

    db = _make_db(tmp_path)
    app = None
    try:
        app = build_web_app(db, {"state_dir": str(tmp_path), "sources": {}},
                            host="127.0.0.1", port=0)
        serve(app)
        host, port = app.server_address[:2]
        _wait_server_ready(app)
        yield app, db, host, port
    finally:
        if app is not None:
            app.shutdown()
            app.server_close()
        db.close()


# --- /api/auth/signup --------------------------------------------------------


def test_signup_first_account_is_admin(web_app):
    """First-ever account via /api/auth/signup → 200, role admin, created."""
    _app, _db, host, port = web_app
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "first@example.com", "password": "pw12345"},
    )
    assert code == 200, f"first signup expected 200, got {code}: {raw[:200]!r}"
    assert parsed and parsed.get("role") == "admin", (
        f"first account must be admin, got: {parsed!r}"
    )
    assert parsed.get("created") is True, f"created flag missing: {parsed!r}"


def test_signup_second_account_is_reader(web_app):
    """Second account (first already exists) → 200, role reader."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "second@example.com", "password": "pw12345"},
    )
    assert code == 200, f"second signup expected 200, got {code}: {raw[:200]!r}"
    assert parsed and parsed.get("role") == "reader", (
        f"second account must be reader, got: {parsed!r}"
    )


def test_signup_duplicate_email_409(web_app):
    """Re-signup of an existing email → 409 'account already exists'."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "first@example.com", "password": "other-pw"},
    )
    assert code == 409, f"duplicate signup expected 409, got {code}: {raw[:200]!r}"
    assert parsed and parsed.get("error") == "account already exists", (
        f"duplicate 409 body must be 'account already exists': {parsed!r}"
    )


def test_signup_blank_email_400(web_app):
    """Blank email → 400."""
    _app, _db, host, port = web_app
    code, _parsed, raw = _http_post(
        host, port, "/api/auth/signup", {"email": "", "password": "pw"})
    assert code == 400, f"blank email expected 400, got {code}: {raw[:200]!r}"


def test_signup_blank_password_400(web_app):
    """Blank password → 400."""
    _app, _db, host, port = web_app
    code, _parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "x@example.com", "password": ""})
    assert code == 400, f"blank password expected 400, got {code}: {raw[:200]!r}"


# --- /api/auth/signin --------------------------------------------------------


def test_signin_valid_200(web_app):
    """Valid credentials → 200 with non-empty session_token + expires_at."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "first@example.com", "password": "pw12345"},
    )
    assert code == 200, f"valid signin expected 200, got {code}: {raw[:200]!r}"
    assert parsed is not None, f"signin body must be JSON: {raw[:200]!r}"
    token = parsed.get("session_token")
    assert isinstance(token, str) and token, (
        f"signin must return a non-empty session_token: {parsed!r}"
    )
    assert "expires_at" in parsed, f"signin body missing expires_at: {parsed!r}"


def test_signin_wrong_password_401(web_app):
    """Wrong password → 401 and NO session_token key in the body."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "first@example.com", "password": "wrong"},
    )
    assert code == 401, f"wrong-password signin expected 401, got {code}: {raw[:200]!r}"
    assert "session_token" not in (parsed or {}), (
        f"401 signin body must not carry session_token: {parsed!r}"
    )


def test_signin_unknown_email_401(web_app):
    """Unknown email → 401."""
    _app, _db, host, port = web_app
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "ghost@example.com", "password": "pw12345"},
    )
    assert code == 401, f"unknown-email signin expected 401, got {code}: {raw[:200]!r}"
    assert "session_token" not in (parsed or {}), (
        f"401 signin body must not carry session_token: {parsed!r}"
    )


# --- /api/auth/signout -------------------------------------------------------


def test_signout_valid_token_200(web_app):
    """Signout with a live Bearer token → 200 {revoked: True}."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    body = _signin(host, port, "first@example.com")
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signout", {},
        headers={"Authorization": f"Bearer {body['session_token']}"})
    assert code == 200, f"signout expected 200, got {code}: {raw[:200]!r}"
    assert parsed and parsed.get("revoked") is True, (
        f"signout body must be {revoked: True}: {parsed!r}"
    )


def test_signout_revoked_token_now_401(web_app):
    """After signout, the revoked token is 401 on /api/me (revocation sticks)."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    body = _signin(host, port, "first@example.com")
    _http_post(host, port, "/api/auth/signout", {},
               headers={"Authorization": f"Bearer {body['session_token']}"})
    code, _parsed, raw = _http_get(
        host, port, "/api/me",
        headers={"Authorization": f"Bearer {body['session_token']}"})
    assert code == 401, (
        f"revoked token on /api/me expected 401, got {code}: {raw[:200]!r}"
    )


def test_signout_no_token_401(web_app):
    """Signout with no Authorization header → 401 (fail-closed)."""
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    code, _parsed, raw = _http_post(host, port, "/api/auth/signout", {})
    assert code == 401, f"signout without token expected 401, got {code}: {raw[:200]!r}"


# --- /api/me -----------------------------------------------------------------


def test_me_valid_token_200(web_app, monkeypatch):
    """Valid Bearer token → 200 {email, role, point_count} (0 on empty KB).

    ``point_count`` must be 0 on an empty collection: the handler must not
    require a reachable Qdrant for the scaffold — the Qdrant count is
    monkeypatched to 0 here, so the test pins the response shape without a
    vector store.
    """
    import digital_twins.mcp.dispatch as dispatch_mod
    import qdrant_client as _qc

    class _StubQdrant:
        def count(self, *_args, **_kwargs):
            return 0

    # No Qdrant is reachable in the unit test: stub the client the /api/me
    # handler will build (via the 005 dispatch factory) so point_count
    # degrades to 0 on the empty collection.
    monkeypatch.setattr(_qc, "QdrantClient", _StubQdrant)
    monkeypatch.setattr(dispatch_mod, "QdrantClient", _StubQdrant,
                        raising=False)
    _app, _db, host, port = web_app
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    body = _signin(host, port, "first@example.com")
    code, parsed, raw = _http_get(
        host, port, "/api/me",
        headers={"Authorization": f"Bearer {body['session_token']}"})
    assert code == 200, f"/api/me expected 200, got {code}: {raw[:200]!r}"
    assert parsed is not None, f"/api/me body must be JSON: {raw[:200]!r}"
    assert parsed.get("email") == "first@example.com", f"me email: {parsed!r}"
    assert parsed.get("role") == "admin", f"me role: {parsed!r}"
    assert parsed.get("point_count") == 0, (
        f"point_count must be 0 on an empty collection: {parsed!r}"
    )


def test_me_point_count_reflects_owner_scope(web_app, monkeypatch):
    """GET /api/me → point_count equals the owner_tag-scoped Qdrant count.

    Pins the installed ``qdrant_client.count(count_filter=...)`` contract on
    the ``/api/me`` path (``_count_points_for_owner``): the owner filter must
    be passed as ``count_filter=``.  The stub mirrors the real client's
    signature and rejects an unknown ``filter=`` kwarg the way the real
    client's ``Unknown arguments`` guard does, so a regression to the old
    kwarg degrades ``point_count`` to 0.
    """
    import digital_twins.mcp.dispatch as dispatch_mod
    import qdrant_client as _qc

    class _CountResult:
        count = 3

    class _StubQdrant:
        def __init__(self, *a, **k):
            pass

        def count(self, collection, count_filter=None, exact=True, **_kw):
            assert not _kw, f"Unknown arguments: {sorted(_kw)}"
            return _CountResult()

    monkeypatch.setattr(_qc, "QdrantClient", _StubQdrant)
    monkeypatch.setattr(dispatch_mod, "QdrantClient", _StubQdrant,
                        raising=False)
    _app, _db, host, port = web_app
    # Point the /api/me handler at a (fake) live Qdrant so it does not
    # short-circuit on "qdrant.url unset".
    _app.config.setdefault("qdrant", {})["url"] = "http://fake-qdrant:6333"
    _http_post(host, port, "/api/auth/signup",
               {"email": "first@example.com", "password": "pw12345"})
    body = _signin(host, port, "first@example.com")
    code, parsed, raw = _http_get(
        host, port, "/api/me",
        headers={"Authorization": f"Bearer {body['session_token']}"})
    assert code == 200, f"/api/me expected 200, got {code}: {raw[:200]!r}"
    assert parsed.get("point_count") == 3, (
        f"point_count must be the owner_tag-scoped Qdrant count (3), "
        f"got {parsed!r}"
    )


def test_me_no_token_401(web_app):
    """GET /api/me with no Authorization header → 401 (bearer gate)."""
    _app, _db, host, port = web_app
    code, _parsed, raw = _http_get(host, port, "/api/me")
    assert code == 401, f"/api/me without token expected 401, got {code}: {raw[:200]!r}"


def test_me_invalid_token_401(web_app):
    """GET /api/me with a garbage Bearer token → 401 (fail-closed)."""
    _app, _db, host, port = web_app
    code, _parsed, raw = _http_get(
        host, port, "/api/me",
        headers={"Authorization": "Bearer not-a-real-token"})
    assert code == 401, f"/api/me with garbage token expected 401, got {code}: {raw[:200]!r}"
