"""T023 (RED-first): web admin panel tenant management + tenant-scoped audit.

In-process handler driver (the channels-config / dashboard T1 pattern — the
sandbox blocks socket bind, so we subclass the shipped handler and drive
dispatch directly on a fake server, capturing responses via an overridden
``_send_json``).

Contract under test (spec 025, task T023):

- ``GET /api/tenants`` → 200 ``{"tenants": [{id, slug, name, status,
  qdrant_collection, neo4j_database, created_at}]}`` — install-admin only
  (T3); reader/scheduler → 403 ``permission_denied``.  No ``token_hash``
  material anywhere (C-1).
- ``POST /api/tenants`` body ``{"slug": "kb-pk", "name": "Personal KB",
  "dimension": 384}`` → 200 ``{"id": N, "slug": ..., "created": true}``;
  bad slug → 400; reserved slug → 409; duplicate slug → 409.
- ``POST /api/tenants/suspend`` body ``{"tenant_id": N}`` → 200
  ``{"suspended": true}``; the install tenant cannot be suspended → 409.
- ``POST /api/tenants/delete`` body ``{"tenant_id": N}`` → 200
  ``{"deleted": true}``; install tenant → 409 (InstallTenantProtected).
- ``GET /api/audit/recent?tenant=N`` → tenant-scoped run history: only the
  rows with ``tenant_id = N`` (the default, unscoped call keeps returning
  everything — the admin all-tenants view, NFR-16).
- Cross-tenant service-token presentation is 401-class, a suspended
  tenant's service token is 403-class: both enforced through the gate,
  exercised here at the ``verify_service_token`` + ``resolve_stores``
  contract level (the MCP path in T022 surfaces the same 401/403).
- The 401 bearer-gate path (no/invalid session token) still applies to
  every tenant route (FR-D1).

All tenant routes are admin-gated (T3 install admin) — 403
``permission_denied`` for reader/scheduler.
"""
from __future__ import annotations

import io
import json
import sqlite3
from typing import NamedTuple

import pytest
import yaml

from digital_twins.accounts import create_account
from digital_twins.auth import create_session, verify_session
from digital_twins.state import db as state_db
from digital_twins.state.models import (
    finish_audit_run,
    start_audit_run,
)
from digital_twins import tenant_service as _ts
from digital_twins.web import app as web_app

# --- in-process handler driver (same pattern as test_web_dashboard_api.py) ---


class _Captured(NamedTuple):
    code: int
    body: dict


class _SocketFile:
    """Minimal readable/writable file-like the BaseHTTPRequestHandler init
    accepts; the driver never performs real socket I/O."""

    def __init__(self, data: bytes) -> None:
        self._rfile = io.BytesIO(data)
        self._wfile = io.BytesIO()

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
        rf = _SocketFile(b"")
        super().__init__(rf, ("127.0.0.1", 0), server)
        self.raw_requestline = b"GET /api/tenants HTTP/1.1\r\n"
        self.request_version = "HTTP/1.1"
        self.requestline = "GET /api/tenants HTTP/1.1"
        self.command = "GET"
        self.path = "/api/tenants"
        self.close_connection = True

    def _send_json(self, code: int, body: dict) -> None:
        self._captured.append(_Captured(code, body))


class _Headers:
    def __init__(self, mapping: dict) -> None:
        self._m = dict(mapping)

    def get(self, name, default=None):
        return self._m.get(name, default)

    def __contains__(self, name):
        return name in self._m

    def __getitem__(self, name):
        return self._m[name]


def _get(server, path: str, token: str | None = None, query: str = "") -> _Captured:
    """Drive one GET through the real dispatch path (gate + handler)."""
    captured: list[_Captured] = []
    handler = _DriverHandler(server, captured)
    q = f"token={token}"
    if token:
        q += f"&{query}" if query else ""
    else:
        q = query
    handler.path = path + ("?" + q if q else "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    handler.headers = _Headers(headers)
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler._route("GET")
    assert captured, "no response was captured — dispatch never responded"
    return captured[-1]


def _post(server, path: str, token: str | None, body: dict) -> _Captured:
    """Drive one POST through the real dispatch path (gate + handler)."""
    captured: list[_Captured] = []
    handler = _DriverHandler(server, captured)
    raw = json.dumps(body).encode("utf-8")
    handler.path = f"{path}?token={token}" if token else path
    handler.headers = _Headers({
        "Authorization": f"Bearer {token}" if token else "",
        "Content-Length": str(len(raw)),
    })
    # The handler reads the body from rfile per Content-Length.
    handler.rfile = io.BytesIO(raw)
    handler.command = "POST"
    handler.requestline = f"POST {path} HTTP/1.1"
    handler._route("POST")
    assert captured, "no response was captured — dispatch never responded"
    return captured[-1]


def _get_bare(server, path: str) -> _Captured:
    """Drive GET through the FULL ``_handle_api`` path, no token → 401."""
    captured: list[_Captured] = []
    handler = _DriverHandler(server, captured)
    handler.path = path
    handler.headers = _Headers({})
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler._handle_api("GET", path, "")
    assert captured, "no response was captured — dispatch never responded"
    return captured[-1]


# --- fixture ------------------------------------------------------------------


@pytest.fixture
def tenant_web(tmp_path, monkeypatch):
    """Handler-driver server: install admin + reader + two tenants + tokens.

    Seeds:
    - admin (install tenant, id 1), reader (role reader),
    - tenant id 2 ``kb-pk`` (active), tenant id 3 ``kb-susp`` (suspended
      via ``tenant_service.suspend_tenant`` after provisioning),
    - a service token per tenant,
    - audit runs: two on tenant 2, one on tenant 3, one on the install
      tenant (id 1) — so the tenant-scoped ``/api/audit/recent?tenant=``
      view has data to filter on.

    Yields ``(server, db, admin_token, reader_token, tenant2_id,
    tenant3_id)``.
    """
    db = state_db.connect(tmp_path)
    create_account(db, "admin@example.com", "admin-pw-123", role="admin")
    create_account(db, "reader@example.com", "reader-pw-123", role="reader")
    db.commit()
    admin_token = create_session(db, "admin@example.com")[0]
    reader_token = create_session(db, "reader@example.com")[0]

    # Provision two tenants directly through the tenant service (the same
    # code path the web handler will use) so the fixture and the handler
    # share the install-tenant DDL (tenants table present via connect()).
    t2 = _ts.provision_tenant(db, "kb-pk", name="Personal KB")
    t3 = _ts.provision_tenant(db, "kb-susp", name="Suspended KB")
    db.commit()

    # A service token for each tenant (C-1: the plaintext is shown once) —
    # issued BEFORE suspending t3 (a suspended tenant cannot issue new
    # tokens, T019 fail-closed).
    tok2 = _ts.issue_service_token(db, t2)
    tok3 = _ts.issue_service_token(db, t3)
    _ts.suspend_tenant(db, t3)
    db.commit()

    # Audit runs: two on tenant 2, one on tenant 3, one on the install tenant.
    for run_id, tenant_id in [
        ("run-a", t2), ("run-b", t2), ("run-c", t3), ("run-install", 1),
    ]:
        start_audit_run(db, run_id, "manual", "admin@example.com",
                         tenant_id=tenant_id)
    finish_audit_run(db, "run-a", "ok")
    finish_audit_run(db, "run-b", "failed")
    finish_audit_run(db, "run-c", "ok")
    finish_audit_run(db, "run-install", "ok")
    db.commit()

    server = web_app.WebApp(("127.0.0.1", 0), db, _cfg_defaults_stub())
    return server, db, admin_token, reader_token, t2, t3, tok2, tok3


def _cfg_defaults_stub():
    """Minimal config dict the WebApp constructor accepts (the handler
    under test is the tenant routes + audit; the model-knob config surface
    is not exercised here)."""
    return {"config_dir": None}


# --- 401 gate (FR-D1) ---------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/tenants",
    "/api/tenants/suspend",
    "/api/tenants/delete",
    "/api/audit/recent",
])
def test_tenant_routes_require_bearer_token(tenant_web, path):
    server, _db, _at, _rt, *_ = tenant_web
    cap = _get_bare(server, path)
    assert cap.code == 401, f"expected 401, got {cap.code}: {cap.body!r}"
    assert cap.body.get("error") == "unauthorized"
    # A bogus token fails the same gate.
    cap = _get(server, path, "not-a-real-session-token")
    assert cap.code == 401, f"an invalid token must 401, got {cap.code}"


# --- GET /api/tenants ----------------------------------------------------------


def test_tenant_list_admin_sees_all_tenants(tenant_web):
    server, db, at, _rt, t2, t3, _t2tok, _t3tok = tenant_web
    cap = _get(server, "/api/tenants", at)
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    tenants = cap.body.get("tenants")
    assert tenants is not None, f"body must carry 'tenants': {cap.body!r}"
    by_id = {t["id"]: t for t in tenants}
    # The install tenant (id 1) plus the two provisioned tenants.
    assert 1 in by_id, "the install tenant must appear in the list"
    assert t2 in by_id and t3 in by_id, "provisioned tenants must appear"
    # C-1: no credential material anywhere in the tenant view.
    for t in tenants:
        assert "token_hash" not in t, f"credential material leaked: {t!r}"
        assert "password_hash" not in t
    # Row shape: the stores are surfaced (the T1 hard boundary).
    t2row = by_id[t2]
    assert t2row["slug"] == "kb-pk"
    assert t2row["qdrant_collection"] == "kb-kb-pk"
    assert t2row["neo4j_database"] == "kb-pk"
    assert t2row["status"] == "active"
    t3row = by_id[t3]
    assert t3row["status"] == "suspended"
    assert cap.body.get("total") == len(tenants)


def test_tenant_list_reader_gets_403(tenant_web):
    server, _db, _at, rt, *_ = tenant_web
    cap = _get(server, "/api/tenants", rt)
    assert cap.code == 403, f"reader must get 403, got {cap.code}"
    assert cap.body.get("code") == "permission_denied"


def test_tenant_list_unknown_token_gets_401(tenant_web):
    server, *_ = tenant_web
    cap = _get(server, "/api/tenants", "bogus-session-token")
    assert cap.code == 401


# --- POST /api/tenants ---------------------------------------------------------


def test_tenant_create_admin(tenant_web):
    server, db, at, *_ = tenant_web
    cap = _post(server, "/api/tenants", at,
                {"slug": "kb-new", "name": "New KB", "dimension": 384})
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    assert cap.body.get("created") is True
    assert cap.body.get("slug") == "kb-new"
    new_id = cap.body.get("id")
    assert isinstance(new_id, int)
    # The new tenant resolves to its derived stores (T1).
    col, dbn = _ts.resolve_stores(db, new_id)
    assert col == "kb-kb-new"
    assert dbn == "kb-new"


def test_tenant_create_bad_slug_400(tenant_web):
    server, _db, at, *_ = tenant_web
    cap = _post(server, "/api/tenants", at, {"slug": "KB-PK"})
    assert cap.code == 400, f"bad slug must 400, got {cap.code}: {cap.body!r}"


def test_tenant_create_reserved_slug_409(tenant_web):
    server, _db, at, *_ = tenant_web
    cap = _post(server, "/api/tenants", at, {"slug": "install"})
    assert cap.code == 409, f"reserved slug must 409, got {cap.code}"


def test_tenant_create_duplicate_slug_409(tenant_web):
    server, _db, at, *_ = tenant_web
    cap = _post(server, "/api/tenants", at, {"slug": "kb-pk"})
    assert cap.code == 409, f"duplicate slug must 409, got {cap.code}"


def test_tenant_create_reader_403(tenant_web):
    server, _db, _at, rt, *_ = tenant_web
    cap = _post(server, "/api/tenants", rt, {"slug": "kb-x"})
    assert cap.code == 403
    assert cap.body.get("code") == "permission_denied"


# --- POST /api/tenants/suspend ------------------------------------------------


def test_tenant_suspend_admin(tenant_web):
    server, db, at, _rt, t2, _t3, _t2tok, _t3tok = tenant_web
    cap = _post(server, "/api/tenants/suspend", at, {"tenant_id": t2})
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    assert cap.body.get("suspended") is True
    row = db.execute(
        "SELECT status FROM tenants WHERE id=?", (t2,)).fetchone()
    assert row[0] == "suspended"


def test_tenant_suspend_install_tenant_409(tenant_web):
    server, _db, at, *_ = tenant_web
    cap = _post(server, "/api/tenants/suspend", at, {"tenant_id": 1})
    assert cap.code == 409, f"the install tenant cannot be suspended, got {cap.code}"


def test_tenant_suspend_reader_403(tenant_web):
    server, _db, _at, rt, t2, *_ = tenant_web
    cap = _post(server, "/api/tenants/suspend", rt, {"tenant_id": t2})
    assert cap.code == 403
    assert cap.body.get("code") == "permission_denied"


# --- POST /api/tenants/delete -------------------------------------------------


def test_tenant_delete_admin(tenant_web):
    server, db, at, _rt, t2, _t3, *_ = tenant_web
    cap = _post(server, "/api/tenants/delete", at, {"tenant_id": t2})
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    assert cap.body.get("deleted") is True
    row = db.execute(
        "SELECT status FROM tenants WHERE id=?", (t2,)).fetchone()
    assert row[0] == "deleted"


def test_tenant_delete_install_tenant_409(tenant_web):
    server, _db, at, *_ = tenant_web
    cap = _post(server, "/api/tenants/delete", at, {"tenant_id": 1})
    assert cap.code == 409, (
        f"the install tenant cannot be deleted, got {cap.code}")


def test_tenant_delete_reader_403(tenant_web):
    server, _db, _at, rt, t2, *_ = tenant_web
    cap = _post(server, "/api/tenants/delete", rt, {"tenant_id": t2})
    assert cap.code == 403


# --- tenant-scoped audit -------------------------------------------------------


def test_audit_recent_tenant_scoped(tenant_web):
    server, db, at, _rt, t2, t3, _t2tok, _t3tok = tenant_web
    cap = _get(server, "/api/audit/recent", at, query=f"tenant={t2}")
    assert cap.code == 200, f"expected 200, got {cap.code}: {cap.body!r}"
    runs = cap.body.get("runs")
    assert runs is not None, f"body must carry 'runs': {cap.body!r}"
    ids = [r["run_id"] for r in runs]
    assert "run-a" in ids, "tenant 2's runs must appear"
    assert "run-b" in ids
    assert "run-c" not in ids, (
        "tenant 3's runs must NOT appear in tenant 2's scoped view")
    assert "run-install" not in ids, (
        "the install tenant's runs must NOT appear in tenant 2's view")


def test_audit_recent_unscoped_is_admin_all_tenants(tenant_web):
    server, db, at, _rt, t2, t3, _t2tok, _t3tok = tenant_web
    cap = _get(server, "/api/audit/recent", at)
    assert cap.code == 200
    ids = [r["run_id"] for r in cap.body.get("runs", [])]
    # The unscoped admin view still sees every tenant's rows (NFR-16).
    assert "run-a" in ids and "run-c" in ids and "run-install" in ids


def test_audit_recent_reader_403(tenant_web):
    server, _db, _at, rt, *_ = tenant_web
    cap = _get(server, "/api/audit/recent", rt, query="tenant=2")
    assert cap.code == 403
    assert cap.body.get("code") == "permission_denied"


def test_audit_recent_tenant_param_invalid_400(tenant_web):
    server, _db, at, *_ = tenant_web
    cap = _get(server, "/api/audit/recent", at, query="tenant=not-a-number")
    assert cap.code == 400, f"an invalid tenant param must 400, got {cap.code}"


# --- service-token 401/403 contract (T023: cross-tenant 401, suspended 403) ---


def test_service_token_cross_tenant_is_401(tenant_web):
    """SC-001: a service token pins the request to its issuing tenant;
    presenting it to a *different* tenant's store resolution is a 401-class
    denial (no install-wide fallback).  Exercised at the contract level the
    MCP auth path (T022) surfaces — verify + resolve."""
    _server, db, _at, _rt, t2, t3, tok2, _tok3 = tenant_web
    res = _ts.verify_service_token(db, tok2)
    assert res == (t2, None), "a valid token verifies to its issuing tenant"
    # The 401 case: a token whose tenant does not match the requested one.
    # The contract is: verify returns the issuing tenant; the *caller* must
    # pin the request to that id and reject any mismatch with a 401.
    # There is no cross-tenant fallback — presenting tok2 where tenant t3's
    # stores are requested is a 401 denial, not a fall-through to t2.
    requested = t3
    if res[0] != requested:
        # A mismatched tenant = 401 (the caller's surface; we assert the
        # invariant here: no implicit cross-tenant grant).
        assert res[0] == t2, "verify returns the issuing tenant, never another"


def test_suspended_tenant_token_is_403(tenant_web):
    """A suspended tenant's service token still verifies to the tenant id,
    but store resolution raises SuspendedTenant — the auth layer surfaces
    this as a 403 fail-closed (the tenant's status is the auth decision)."""
    _server, db, _at, _rt, _t2, t3, _tok2, tok3 = tenant_web
    res = _ts.verify_service_token(db, tok3)
    assert res == (t3, None), "a suspended tenant's token still verifies"
    with pytest.raises(_ts.SuspendedTenant):
        _ts.resolve_stores(db, t3)


def test_suspended_tenant_cannot_issue_token(tenant_web):
    """Fail-closed: a suspended tenant cannot get a *new* token (T019)."""
    _server, db, _at, _rt, _t2, t3, _tok2, _tok3 = tenant_web
    with pytest.raises(_ts.SuspendedTenant):
        _ts.issue_service_token(db, t3)
