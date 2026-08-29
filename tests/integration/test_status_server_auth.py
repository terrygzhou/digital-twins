"""T013 — StatusServer auth_checker (C-5 / R8).

Red-first per the task brief. These tests pin the 003 contract for the
``StatusServer`` auth hook (contracts/scheduler.md):

- A ``StatusServer`` **without** ``auth_checker`` (002's callers) returns 200
  on ``/status`` with no auth check (C-5 backward-compat).
- With a checker: no credential -> 401, service token -> 200, a personal
  token -> 200, a session token -> 200, an unknown Bearer -> 401.

These tests will FAIL initially because the ``auth_checker`` kwarg does not
exist yet on ``StatusServer.__init__``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from digital_twins.accounts import create_account
from digital_twins.auth import (
    create_personal_token,
    create_session,
    verify_personal_token,
    verify_session,
)
from digital_twins.scheduler.status import StatusServer
from digital_twins.state import migrations

_SERVICE_TOKEN = "svc-abcdef0123456789abcdef0123456789"
_SERVICE_TOKEN_ENV = "DT_SERVICE_TOKEN"


# --- helpers ---------------------------------------------------------------


def _make_db(tmp_path, *, check_same_thread: bool = False):
    """Migrated db (v3) with personal_tokens + sessions tables.

    ``check_same_thread=False`` (the default) mirrors 001's own
    ``connect`` (state/db.py), which is what the real ``cli serve`` passes
    to the auth_checker: the verifiers (``verify_personal_token`` /
    ``verify_session``) run on the handler thread, so the captured ``db``
    must be usable cross-thread. (The StatusServer also reopens the file
    with ``check_same_thread=False`` for its own ``/status`` payload reads.)
    """
    path = tmp_path / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrations.migrate(conn)
    return conn


def _seed_account(db, email: str) -> None:
    """Create an account row (FK target for personal_tokens / sessions)."""
    create_account(db, email, "test-password-000")


def _config() -> dict:
    return {"state_dir": "/tmp/unused", "sources": {}}


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


def _get_status(server, *, headers=None):
    """GET /status; return (status_code, parsed_json_or_None, raw_body)."""
    host, port = server.server_address[:2]
    req = urllib.request.Request(
        f"http://{host}:{port}/status", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            return resp.status, parsed, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed, body


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- C-5 backward-compat: no auth_checker -> 200, no auth check -------------


def test_no_auth_checker_returns_200_without_auth(tmp_path):
    """A StatusServer built WITHOUT auth_checker (002's callers) returns 200
    on /status with no Authorization header at all — the 002 no-auth behavior
    is preserved byte-for-byte (C-5 backward-compat)."""
    db = _make_db(tmp_path)
    server = StatusServer(("127.0.0.1", 0), db, _config())
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        # No Authorization header: still 200 (002 behavior).
        code, parsed, _ = _get_status(server)
        assert code == 200, f"expected 200 with no checker, got {code}"
        assert isinstance(parsed, dict)
        assert set(parsed) == {"uptime_s", "schedules", "last_run",
                                "queue_depth"}
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_no_auth_checker_ignores_bogus_bearer(tmp_path):
    """With no checker, even a bogus Bearer is ignored — 002 had no auth, so
    any Authorization header is a no-op. 200."""
    db = _make_db(tmp_path)
    server = StatusServer(("127.0.0.1", 0), db, _config())
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        code, parsed, _ = _get_status(
            server, headers=_bearer("totally-bogus-token"))
        assert code == 200, (
            f"expected 200 (no checker ignores auth), got {code}")
        assert isinstance(parsed, dict)
    finally:
        server.shutdown()
        server.server_close()
        db.close()


# --- with a checker: service / personal / session / unknown -----------------


def _make_checker(db):
    """Mirror cli._auth_checker's logic (kept local so the integration test
    does not depend on the cli import chain, but exercises the same order)."""
    import hmac

    def check(headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False  # 401: no credential
        token = auth[len("Bearer "):].strip()
        # 1) shared service token (constant-time compare)
        service = os.environ.get(_SERVICE_TOKEN_ENV)
        if service and hmac.compare_digest(token, service):
            return True
        # 2) personal token
        if verify_personal_token(db, token):
            return True
        # 3) session token
        if verify_session(db, token):
            return True
        # 4) unknown Bearer -> 401
        return False

    return check


def test_service_token_200(tmp_path, monkeypatch):
    """A Bearer matching DT_SERVICE_TOKEN (constant-time) -> 200."""
    db = _make_db(tmp_path)
    monkeypatch.setenv(_SERVICE_TOKEN_ENV, _SERVICE_TOKEN)
    server = StatusServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        code, parsed, _ = _get_status(server, headers=_bearer(_SERVICE_TOKEN))
        assert code == 200, f"service token should be 200, got {code}"
        assert isinstance(parsed, dict)
        assert "uptime_s" in parsed
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_personal_token_200(tmp_path, monkeypatch):
    """A valid personal token (pbkdf2 in personal_tokens) -> 200."""
    db = _make_db(tmp_path)
    monkeypatch.delenv(_SERVICE_TOKEN_ENV, raising=False)
    _seed_account(db, "alice@example.com")
    _token_id, personal = create_personal_token(db, "alice@example.com")
    server = StatusServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        code, parsed, _ = _get_status(server, headers=_bearer(personal))
        assert code == 200, f"personal token should be 200, got {code}"
        assert isinstance(parsed, dict)
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_session_token_200(tmp_path, monkeypatch):
    """A valid, un-revoked, un-expired session token -> 200."""
    db = _make_db(tmp_path)
    monkeypatch.delenv(_SERVICE_TOKEN_ENV, raising=False)
    _seed_account(db, "bob@example.com")
    _session, _expires = create_session(db, "bob@example.com")
    server = StatusServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        code, parsed, _ = _get_status(server, headers=_bearer(_session))
        assert code == 200, f"session token should be 200, got {code}"
        assert isinstance(parsed, dict)
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_no_credential_401(tmp_path, monkeypatch):
    """No Authorization header -> 401 (the checker returns False)."""
    db = _make_db(tmp_path)
    monkeypatch.delenv(_SERVICE_TOKEN_ENV, raising=False)
    server = StatusServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        code, parsed, _ = _get_status(server)  # no headers
        assert code == 401, f"no credential should be 401, got {code}"
        assert parsed is None or isinstance(parsed, dict)
        if parsed is not None:
            assert "error" in parsed
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_unknown_bearer_401(tmp_path, monkeypatch):
    """A Bearer token that matches none of service / personal / session ->
    401 (not 403, per the contract: no mutating /status route in 003)."""
    db = _make_db(tmp_path)
    monkeypatch.delenv(_SERVICE_TOKEN_ENV, raising=False)
    server = StatusServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        code, parsed, _ = _get_status(
            server, headers=_bearer("not-a-real-token"))
        assert code == 401, f"unknown Bearer should be 401, got {code}"
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_checker_false_vs_string_401_vs_403(tmp_path, monkeypatch):
    """Directly exercise the return-shape contract (R8):
    - checker returns False  -> 401 "unauthorized"
    - checker returns a str  -> 403 with that string as the body error
    - checker returns True   -> 200
    This pins the handler's mapping independent of the cli's checker."""
    db = _make_db(tmp_path)

    # A checker that decides by the token value:
    def pick(headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[len("Bearer "):].strip()
        if token == "allow":
            return True
        if token == "deny":
            return "denied: known credential is suspended"
        return False  # unknown

    server = StatusServer(
        ("127.0.0.1", 0), db, _config(), auth_checker=pick)
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        # True -> 200
        code, parsed, _ = _get_status(server, headers=_bearer("allow"))
        assert code == 200, f"True should be 200, got {code}"

        # str -> 403 with that string as the body error
        code, parsed, _ = _get_status(server, headers=_bearer("deny"))
        assert code == 403, f"str should be 403, got {code}"
        assert parsed is not None
        assert parsed.get("error") == "denied: known credential is suspended"

        # False -> 401 "unauthorized"
        code, parsed, _ = _get_status(server, headers=_bearer("nope"))
        assert code == 401, f"False should be 401, got {code}"
        assert parsed is not None
        assert "error" in parsed
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def test_unknown_path_still_404_with_checker(tmp_path, monkeypatch):
    """The 404 path is unchanged even when a checker is installed."""
    db = _make_db(tmp_path)
    server = StatusServer(
        ("127.0.0.1", 0), db, _config(),
        auth_checker=_make_checker(db))
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        req = urllib.request.Request(
            f"http://{host}:{port}/nonexistent",
            headers=_bearer(_SERVICE_TOKEN))
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5.0)
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        db.close()
