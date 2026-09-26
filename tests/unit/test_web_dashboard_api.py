"""dashboard-left-column T1: web admin dashboard API contract (RED-first).

In-process handler driver (the channels-config ledger ruling): the
sandbox blocks socket bind (``WebBindError`` at ``WebApp.__init__``),
so instead of the ``http.client`` harness used in
tests/unit/test_web_channels_api.py we subclass the shipped handler and
drive ``_dispatch_rest`` / ``_handle_api`` directly on a fake server
instance, capturing responses via an overridden ``_send_json``.  This
mirrors the channels-config T3 in-process battery and exercises the
same dispatch + handler code paths:

- ``GET /api/dashboard/accounts`` → ``{"accounts":[{email, role,
  created_at, last_active}], "total": N}`` (no ``password_hash`` —
  FR-D3).
- ``GET /api/dashboard/jobs`` → ``{"schedules":[...],
  "recent_runs":[...]}`` (most-recent-first).
- ``GET /api/dashboard/models`` → the effective model-knob view with
  ``*_set`` credential booleans (no credential values — FR-D3).

All three are admin-gated (403 ``permission_denied`` for
scheduler/reader, FR-D2).  The 401 bearer-gate path (FR-D1) is
exercised by driving ``_handle_api`` with a missing/invalid token.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from digital_twins.accounts import create_account
from digital_twins.auth import create_session, verify_session
from digital_twins.scheduler.schedules import create_schedule
from digital_twins.state import db as state_db
from digital_twins.state.models import finish_audit_run, start_audit_run
from digital_twins.web import app as web_app

# Obviously-fake credential values (FR-D3/FR-004 safe).
FAKE_LLM_KEY = "sk-fake-llm-key"


# --- in-process handler driver (socket bind is blocked in this sandbox) ------


class _Captured(NamedTuple):
    code: int
    body: dict


class _SocketFile:
    """Minimal readable/writable file-like the BaseHTTPRequestHandler init
    accepts as the socket's makefile() result — the driver never performs a
    real socket I/O, only direct handler invocation."""

    def __init__(self, data: bytes) -> None:
        self._rfile = io.BytesIO(data)
        self._wfile = io.BytesIO()
        self._rfile.readline = self._rfile.readline
        self._wfile.write = self._wfile.write

    def makefile(self, *a, **k):
        return self._rfile

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        self._rfile.close()
        self._wfile.close()


class _DriverHandler(web_app._WebAppHandler):
    """``_WebAppHandler`` with response capture instead of socket writes."""

    def __init__(self, server, captured: list[_Captured]) -> None:
        self._captured = captured
        # BaseHTTPRequestHandler.__init__ takes a (request, client_address,
        # server) triple and parses ``raw_requestline`` from the request's
        # makefile().  The driver never runs the socket request loop —
        # handlers are invoked directly — so ``request.makefile`` returns a
        # real empty bytes stream (a MagicMock would make ``readline`` a
        # MagicMock and clobber the pre-set ``raw_requestline``).  The
        # per-call method/path are re-filled in ``_get`` / ``_get_bare``.
        rf = _SocketFile(b"")
        super().__init__(rf, ("127.0.0.1", 0), server)
        self.raw_requestline = b"GET /api/dashboard/accounts HTTP/1.1\r\n"
        # BaseHTTPRequestHandler.parse_request sets these; the driver fills
        # them so the send_response / log_request path has what it needs.
        self.request_version = "HTTP/1.1"
        self.requestline = "GET /api/dashboard/accounts HTTP/1.1"
        self.command = "GET"
        self.path = "/api/dashboard/accounts"
        self.close_connection = True

    def _send_json(self, code: int, body: dict) -> None:
        self._captured.append(_Captured(code, body))



class _Headers:
    """Minimal request-header mapping (dict-backed) for the driver — the
    handler only calls ``headers.get(name, default)``."""

    def __init__(self, mapping: dict) -> None:
        self._m = dict(mapping)

    def get(self, name, default=None):
        return self._m.get(name, default)

    def __contains__(self, name):
        return name in self._m

    def __getitem__(self, name):
        return self._m[name]

def _get(server, path: str, token: str | None = None) -> _Captured:
    """Drive one GET through the real dispatch path (gate + handler)."""
    captured: list[_Captured] = []
    handler = _DriverHandler(server, captured)
    # The driver routes via the ?token= fallback (the handler reads the
    # token from the Bearer header OR the ?token= query, FR-017) so the
    # request line + query carry everything the gate needs.
    query = f"token={token}" if token else ""
    handler.path = path + ("?" + query if query else "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    handler.headers = _Headers(headers)
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler._route("GET")
    assert captured, "no response was captured — dispatch never responded"
    return captured[-1]


def _get_bare(server, path: str) -> _Captured:
    """Drive GET through the FULL ``_handle_api`` path (bearer gate runs)
    with NO Authorization header — the gate must 401 before dispatch."""
    captured: list[_Captured] = []
    handler = _DriverHandler(server, captured)
    handler.path = path
    handler.headers = _Headers({})
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler._handle_api("GET", path, "")
    assert captured, "no response was captured — dispatch never responded"
    return captured[-1]


# --- fixture ----------------------------------------------------------------

@pytest.fixture
def dashboard_web(tmp_path, monkeypatch):
    """Handler-driver server with admin + reader + scheduler accounts.

    Seeds two audit runs (one ok, one failed) and one schedule so the
    jobs route has real data.  Yields
    (server, db, admin_token, reader_token, sched_token, config_dir).
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # kb.local.yml pins the model knobs the models route reports (effective
    # view source).  The LLM credential is set as an env var so the
    # ``*_set`` booleans can be asserted without leaking values.
    local_yaml = {
        "llm": {"endpoint": "http://llm.local:8080", "model": "test-model"},
        "embedding": {"model": "test-embed-model"},
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(local_yaml, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_LLM__API_KEY", FAKE_LLM_KEY)

    db = state_db.connect(tmp_path)
    create_account(db, "admin@example.com", "admin-pw-123", role="admin")
    create_account(db, "reader@example.com", "reader-pw-123", role="reader")
    create_account(db, "sched@example.com", "sched-pw-123", role="scheduler")
    db.commit()
    admin_token = create_session(db, "admin@example.com")[0]
    reader_token = create_session(db, "reader@example.com")[0]
    sched_token = create_session(db, "sched@example.com")[0]

    # Seed two audit runs (older first so most-recent-first is observable).
    start_audit_run(db, "run-0001", trigger="manual",
                    scheduled_by="admin@example.com")
    finish_audit_run(db, "run-0001", "ok", {"fs": 3})
    start_audit_run(db, "run-0002", trigger="manual",
                    scheduled_by="admin@example.com")
    finish_audit_run(db, "run-0002", "failed", {})

    # Seed one schedule (batch-job registry).
    create_schedule(db, owner="admin@example.com", source="fs", preset="daily")
    db.commit()

    # A WebApp instance carries db/config/_db_lock; its socket bind is
    # bypassed: build the attributes directly, no ThreadingHTTPServer
    # __init__ (the ledger's in-process pattern).
    import threading
    server = web_app.WebApp.__new__(web_app.WebApp)
    server.config = web_app._merge_defaults(
        {"state_dir": str(tmp_path), "sources": {},
         "config_dir": str(config_dir)})
    server.start_time = 0.0
    server._db_lock = threading.Lock()
    server.qdrant_client = None
    server.db = web_app._open_same_db(db, check_same_thread=False)

    try:
        yield (server, db, admin_token, reader_token, sched_token,
               config_dir)
    finally:
        db.close()


# =============================================================================
# GET /api/dashboard/accounts
# =============================================================================

def test_dashboard_accounts_admin_200_shape(dashboard_web):
    """GET as admin → 200 with the accounts list + total (FR-D2/D3)."""
    server, _db, admin_token, _rt, _st, _cd = dashboard_web

    cap = _get(server, "/api/dashboard/accounts", admin_token)
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    body = cap.body
    assert "accounts" in body, f"'accounts' key missing: {list(body)!r}"
    assert "total" in body, f"'total' key missing: {list(body)!r}"
    assert body["total"] == 3, (
        f"'total' must count all accounts (3 seeded), got {body['total']!r}"
    )
    by_email = {a["email"]: a for a in body["accounts"]}
    assert by_email["admin@example.com"]["role"] == "admin"
    assert by_email["reader@example.com"]["role"] == "reader"
    assert by_email["sched@example.com"]["role"] == "scheduler"
    # FR-D3: no credential material on any row.
    for acct in body["accounts"]:
        assert "password_hash" not in acct, (
            f"password_hash leaked into account row: {acct!r}"
        )
        for field in ("email", "role", "created_at", "last_active"):
            assert field in acct, f"account row missing {field!r}: {acct!r}"


def test_dashboard_accounts_non_admin_403(dashboard_web):
    """GET as reader + scheduler → 403 permission_denied (FR-D2)."""
    server, _db, _at, reader_token, sched_token, _cd = dashboard_web
    for token in (reader_token, sched_token):
        cap = _get(server, "/api/dashboard/accounts", token)
        assert cap.code == 403, f"expected 403, got {cap.code}: {cap.body!r}"
        assert cap.body.get("error") == "permission_denied", (
            f"403 body must carry error=permission_denied, got {cap.body!r}"
        )


# =============================================================================
# GET /api/dashboard/jobs
# =============================================================================

def test_dashboard_jobs_admin_200_shape(dashboard_web):
    """GET as admin → 200 with schedules + most-recent-first runs."""
    server, _db, admin_token, _rt, _st, _cd = dashboard_web

    cap = _get(server, "/api/dashboard/jobs", admin_token)
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    body = cap.body
    assert "schedules" in body, f"'schedules' key missing: {list(body)!r}"
    assert "recent_runs" in body, f"'recent_runs' key missing: {list(body)!r}"
    # The seeded schedule is present.
    scheds = body["schedules"]
    assert len(scheds) == 1, f"expected 1 seeded schedule, got {len(scheds)}"
    assert scheds[0]["source"] == "fs"
    assert scheds[0]["preset"] == "daily"
    # recent_runs: most-recent-first → run-0002 (the later one) first.
    runs = body["recent_runs"]
    assert len(runs) == 2, f"expected 2 seeded runs, got {len(runs)}"
    assert runs[0]["run_id"] == "run-0002", (
        f"most-recent run must be first, got {runs[0]['run_id']!r}"
    )
    assert runs[0]["status"] == "failed"
    assert runs[1]["run_id"] == "run-0001"
    assert runs[1]["status"] == "ok"


def test_dashboard_jobs_non_admin_403(dashboard_web):
    """GET as reader + scheduler → 403 permission_denied (FR-D2)."""
    server, _db, _at, reader_token, sched_token, _cd = dashboard_web
    for token in (reader_token, sched_token):
        cap = _get(server, "/api/dashboard/jobs", token)
        assert cap.code == 403, f"expected 403, got {cap.code}: {cap.body!r}"
        assert cap.body.get("error") == "permission_denied", (
            f"403 body must carry error=permission_denied, got {cap.body!r}"
        )


# =============================================================================
# GET /api/dashboard/models
# =============================================================================

def test_dashboard_models_admin_200_shape(dashboard_web):
    """GET as admin → 200 with the effective model-knob view (FR-D3)."""
    server, _db, admin_token, _rt, _st, _cd = dashboard_web

    cap = _get(server, "/api/dashboard/models", admin_token)
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    body = cap.body
    assert "models" in body, f"'models' key missing: {list(body)!r}"
    models = body["models"]
    # The effective llm endpoint/model from kb.local.yml.
    assert models["llm"]["endpoint"] == "http://llm.local:8080"
    assert models["llm"]["model"] == "test-model"
    # FR-D3: the credential is a boolean flag, the value is never returned.
    assert models["llm"]["api_key_set"] is True, (
        f"api_key_set must be True (KB_LLM__API_KEY set), got "
        f"{models['llm'].get('api_key_set')!r}"
    )
    assert "api_key" not in models["llm"], (
        f"credential value leaked into models view: {models['llm']!r}"
    )
    # embedding model from kb.local.yml.
    assert models["embedding"]["model"] == "test-embed-model"
    # The credential value must NOT appear anywhere in the response body.
    assert FAKE_LLM_KEY not in json.dumps(body), (
        "credential value leaked into response (FR-D3 violation)"
    )


def test_dashboard_models_non_admin_403(dashboard_web):
    """GET as reader + scheduler → 403 permission_denied (FR-D2)."""
    server, _db, _at, reader_token, sched_token, _cd = dashboard_web
    for token in (reader_token, sched_token):
        cap = _get(server, "/api/dashboard/models", token)
        assert cap.code == 403, f"expected 403, got {cap.code}: {cap.body!r}"
        assert cap.body.get("error") == "permission_denied", (
            f"403 body must carry error=permission_denied, got {cap.body!r}"
        )


# =============================================================================
# FR-D1: bearer gate (401 unauthenticated) on all three routes
# =============================================================================

@pytest.mark.parametrize("path", [
    "/api/dashboard/accounts",
    "/api/dashboard/jobs",
    "/api/dashboard/models",
])
def test_dashboard_unauthenticated_401(dashboard_web, path):
    """No bearer token → 401 unauthorized (FR-D1, the bearer gate runs
    before any dashboard handler; driven through the full _handle_api
    path so the gate is what produces the 401)."""
    server, _db, _at, _rt, _st, _cd = dashboard_web

    cap = _get_bare(server, path)
    assert cap.code == 401, f"expected 401, got {cap.code}: {cap.body!r}"
    assert cap.body.get("error") == "unauthorized", (
        f"401 body must carry error=unauthorized, got {cap.body!r}"
    )
    # A bogus token must fail the same gate (never a 404 / 500).
    cap = _get(server, path, "not-a-real-session-token")
    assert cap.code == 401, (
        f"an invalid bearer token must 401, got {cap.code}: {cap.body!r}"
    )
    # Sanity: the gate accepts the seeded admin session (verify_session is
    # the same helper the gate uses).
    assert verify_session(_db, _at) is not None
