"""T014 — web/server.py /signup + /signin (session tokens).

Red-first per the task brief and constitution III (Test-First).

These tests pin the 003 contract for the minimal web UI credential
endpoints (contracts/scheduler.md, "Web UI credential endpoints"):

- ``POST /signup`` with ``{email, password}`` on an empty DB returns
  ``200 {"email", "role", "created":true}`` with ``role=admin``.
- A second ``POST /signup`` returns ``role=reader``.
- A duplicate email returns ``409 {"error":"account already exists"}``.
- ``POST /signin`` with valid credentials returns
  ``200 {"session_token", "expires_at"}`` where the token is accepted
  by the C-5 ``auth_checker`` on a subsequent request.
- ``POST /signin`` with a bad password returns ``401``.

These tests will FAIL initially because the web server endpoints do not
exist yet (``digital_twins/web/server.py`` is a T001 scaffold stub).
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from digital_twins.accounts import create_account, get_role
from digital_twins.auth import create_session, verify_session
from digital_twins.state import migrations


# --- helpers ---------------------------------------------------------------


def _make_db(tmp_path, *, check_same_thread: bool = False):
    """Migrated db (v3) with personal_tokens + sessions tables."""
    path = tmp_path / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrations.migrate(conn)
    return conn


def _config() -> dict:
    return {"state_dir": "/tmp/unused", "sources": {}}


def _http_post(host: str, port: int, path: str, body: dict,
               headers: dict | None = None) -> tuple[int, dict]:
    """POST JSON to ``http://host:port/path``; return (status_code, parsed_json)."""
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        conn.request("POST", path, body=json.dumps(body), headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        return resp.status, parsed
    finally:
        conn.close()


def _http_get(host: str, port: int, path: str,
              headers: dict | None = None) -> tuple[int, dict | None]:
    """GET ``http://host:port/path``; return (status_code, parsed_json_or_None)."""
    req = urllib.request.Request(
        f"http://{host}:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed


def _wait_server_ready(server, timeout=5.0):
    """Poll /status until the server accepts connections (any HTTP code)."""
    host, port = server.server_address[:2]
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}/status", timeout=1.0
            ) as resp:
                resp.read()
            return
        except urllib.error.HTTPError:
            return  # server answered; any HTTP code means it is up
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _make_checker(db):
    """Mirror cli._auth_checker's logic (kept local so the integration test
    does not depend on the cli import chain, but exercises the same order)."""
    import hmac
    import os

    def check(headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False  # 401: no credential
        token = auth[len("Bearer "):].strip()
        # 1) shared service token (constant-time compare)
        service = os.environ.get("DT_SERVICE_TOKEN")
        if service and hmac.compare_digest(token, service):
            return True
        # 2) personal token
        from digital_twins.auth import verify_personal_token
        if verify_personal_token(db, token):
            return True
        # 3) session token
        if verify_session(db, token):
            return True
        # 4) unknown Bearer -> 401
        return False

    return check


# --- /signup on an empty DB returns role=admin -----------------------------


def test_signup_first_is_admin(tmp_path):
    """POST /signup on an empty DB returns 200 with role=admin."""
    from digital_twins.web.server import WebServer

    db = _make_db(tmp_path)
    server = WebServer(("127.0.0.1", 0), db, _config())
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        code, parsed = _http_post(
            host, port, "/signup",
            {"email": "first@example.com", "password": "first-pw-123"})
        assert code == 200, f"expected 200, got {code}: {parsed}"
        assert parsed is not None
        assert parsed.get("email") == "first@example.com"
        assert parsed.get("role") == "admin"
        assert parsed.get("created") is True

        # verify the role in the DB
        assert get_role(db, "first@example.com") == "admin"
    finally:
        server.shutdown()
        server.server_close()
        db.close()


# --- second /signup returns role=reader -------------------------------------


def test_signup_second_is_reader(tmp_path):
    """A second POST /signup returns 200 with role=reader."""
    from digital_twins.web.server import WebServer

    db = _make_db(tmp_path)
    server = WebServer(("127.0.0.1", 0), db, _config())
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        # first signup
        code1, parsed1 = _http_post(
            host, port, "/signup",
            {"email": "first@example.com", "password": "first-pw-123"})
        assert code1 == 200, f"first signup expected 200, got {code1}"
        assert parsed1.get("role") == "admin"

        # second signup
        code2, parsed2 = _http_post(
            host, port, "/signup",
            {"email": "second@example.com", "password": "second-pw-456"})
        assert code2 == 200, f"second signup expected 200, got {code2}"
        assert parsed2 is not None
        assert parsed2.get("email") == "second@example.com"
        assert parsed2.get("role") == "reader"
        assert parsed2.get("created") is True

        # verify the role in the DB
        assert get_role(db, "second@example.com") == "reader"
    finally:
        server.shutdown()
        server.server_close()
        db.close()


# --- duplicate email returns 409 --------------------------------------------


def test_signup_duplicate_email_409(tmp_path):
    """A duplicate POST /signup returns 409 with the named error."""
    from digital_twins.web.server import WebServer

    db = _make_db(tmp_path)
    server = WebServer(("127.0.0.1", 0), db, _config())
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        # first signup
        code1, parsed1 = _http_post(
            host, port, "/signup",
            {"email": "dup@example.com", "password": "first-pw-123"})
        assert code1 == 200, f"first signup expected 200, got {code1}"

        # duplicate signup
        code2, parsed2 = _http_post(
            host, port, "/signup",
            {"email": "dup@example.com", "password": "other-pw-456"})
        assert code2 == 409, f"duplicate signup expected 409, got {code2}"
        assert parsed2 is not None
        assert "account already exists" in parsed2.get("error", "")

        # only one row in the DB
        count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        assert count == 1, f"expected 1 account row, got {count}"
    finally:
        server.shutdown()
        server.server_close()
        db.close()


# --- /signin with valid credentials returns a session token -----------------


def test_signin_valid_returns_session_token(tmp_path):
    """POST /signin with valid credentials returns 200 with a session_token
    that the C-5 auth_checker accepts on a subsequent request."""
    from digital_twins.web.server import WebServer

    db = _make_db(tmp_path)
    create_account(db, "alice@example.com", "alice-pw-123")
    server = WebServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        code, parsed = _http_post(
            host, port, "/signin",
            {"email": "alice@example.com", "password": "alice-pw-123"})
        assert code == 200, f"expected 200, got {code}: {parsed}"
        assert parsed is not None
        session_token = parsed.get("session_token")
        assert session_token is not None, "session_token missing from response"
        assert isinstance(session_token, str)
        assert len(session_token) > 0

        # expires_at must be present
        assert "expires_at" in parsed

        # The session token must be accepted by the C-5 auth_checker on a
        # subsequent request to /status.
        code_status, parsed_status = _http_get(
            host, port, "/status",
            headers={"Authorization": f"Bearer {session_token}"})
        assert code_status == 200, (
            f"session token should be accepted by auth_checker, "
            f"got {code_status}: {parsed_status}"
        )

        # Also verify directly via the auth module
        assert verify_session(db, session_token) == "alice@example.com"
    finally:
        server.shutdown()
        server.server_close()
        db.close()


# --- /signin with a bad password returns 401 ---------------------------------


def test_signin_bad_password_401(tmp_path):
    """POST /signin with a wrong password returns 401."""
    from digital_twins.web.server import WebServer

    db = _make_db(tmp_path)
    create_account(db, "bob@example.com", "correct-pw-123")
    server = WebServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        code, parsed = _http_post(
            host, port, "/signin",
            {"email": "bob@example.com", "password": "wrong-pw-456"})
        assert code == 401, f"expected 401, got {code}: {parsed}"
    finally:
        server.shutdown()
        server.server_close()
        db.close()
