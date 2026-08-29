"""T013 [006-web-app] RED: /api/audit/recent — per-user scoping.

These tests pin ``GET /api/audit/recent`` (FR-010, SC-005, NFR-16) against
the WebApp scaffold.  The handler does NOT exist yet (T014 lands it): the
T004 scaffold's bearer gate passes for a valid session token and its
``_dispatch_rest`` falls through to a clean JSON 404 for the
unimplemented route.  So every behavioural test in this file fails with
``404`` — not a fixture error, not an auth error — which is the honest RED
the constitution demands.  The no/invalid-token test (test 13) is the
auth-boundary guard and is already green from T004.

Harness pattern mirrors ``tests/unit/test_web_app_kb.py``: migrated v3 state
DB in ``tmp_path``, ``build_web_app`` on 127.0.0.1:0, ``serve``, requests
driven with ``http.client``.  ``audit_runs`` is the 001/002/004 audit
table (unchanged schema — T014 must not add a migration).
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import uuid

import pytest

from digital_twins.accounts import create_account
from digital_twins.auth import create_session
from digital_twins.state import db as state_db

_READY_TIMEOUT_S = 5.0

# --- fixture ------------------------------------------------------------------


def _make_db(tmp_path):
    """Migrated v3 state DB (accounts / sessions / audit_runs / highwater)."""
    return state_db.connect(tmp_path)


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
    raise RuntimeError(f"server at {host}:{port} not ready within {timeout}s")


def _http_get(host: str, port: int, path: str,
              headers: dict | None = None) -> tuple[int, dict | None, bytes]:
    """GET ``http://host:port`` + ``path`` via ``http.client``."""
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


@pytest.fixture
def web_app(tmp_path):
    """Build + serve the 006 WebApp on 127.0.0.1:0 with a v3 state DB.

    Mirrors the fixture in ``test_web_app_kb.py``, but seeded with the
    SC-005 cast: ``admin@example.com`` (first row → admin, R7),
    ``alice@example.com`` (reader), and ``bob@example.com`` (reader) — each
    with a live session token.  Yields ``(app, db, host, port, tokens)``
    where ``tokens`` maps email → session token.
    """
    from digital_twins.web.app import build_web_app, serve

    db = _make_db(tmp_path)
    try:
        # R7: first account row becomes admin; the rest are readers.
        create_account(db, "admin@example.com", "admin-pw-123")
        create_account(db, "alice@example.com", "alice-pw-123")
        create_account(db, "bob@example.com", "bob-pw-123")
        tokens = {
            email: create_session(db, email)[0]
            for email in ("admin@example.com", "alice@example.com",
                          "bob@example.com")
        }

        cfg = {"state_dir": str(tmp_path), "sources": {}}
        app = build_web_app(db, cfg, host="127.0.0.1", port=0)
        serve(app)

        host, port = app.server_address[:2]
        _wait_server_ready(app)

        yield app, db, host, port, tokens
    finally:
        app.shutdown()
        app.server_close()
        db.close()


# --- helpers ------------------------------------------------------------------


def _audit_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]


def _seed_audit_row(db, started_at: str, scheduled_by: str, trigger: str,
                    status: str = "ok", run_id: str | None = None,
                    per_source_counts: dict | None = None) -> str:
    """Seed one ``audit_runs`` row; returns its run_id."""
    import json as _json

    run_id = run_id or str(uuid.uuid4())
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, "
        "trigger, scheduled_by, per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, started_at, started_at, status, trigger, scheduled_by,
         _json.dumps(per_source_counts or {})),
    )
    db.commit()
    return run_id


# --- 9. non-admin sees only own rows --------------------------------------------


def test_audit_recent_non_admin_only_own_rows(web_app):
    """Non-admin alice GET /api/audit/recent?limit=10 → ONLY rows where
    scheduled_by=alice (NFR-16); bob's rows and system rows never leak.

    Seeded: 3 alice rows (one trigger='web'), 2 bob rows, 1 system row.

    RED: the route is unimplemented (T014) → the scaffold's clean JSON 404.
    """
    _app, db, host, port, tokens = web_app
    _seed_audit_row(db, "2026-01-01T00:00:00+00:00", "alice@example.com",
                    "manual")
    _seed_audit_row(db, "2026-01-02T00:00:00+00:00", "alice@example.com",
                    "web")
    _seed_audit_row(db, "2026-01-03T00:00:00+00:00", "alice@example.com",
                    "schedule")
    _seed_audit_row(db, "2026-01-04T00:00:00+00:00", "bob@example.com",
                    "manual")
    _seed_audit_row(db, "2026-01-05T00:00:00+00:00", "bob@example.com",
                    "web")
    _seed_audit_row(db, "2026-01-06T00:00:00+00:00", "system", "schedule")

    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent?limit=10",
        headers={"Authorization": f"Bearer {tokens['alice@example.com']}"})
    assert code == 200, (
        f"GET /api/audit/recent?limit=10 (non-admin) expected 200, "
        f"got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    rows = parsed.get("rows")
    assert isinstance(rows, list), f"'rows' must be a list: {parsed!r}"
    assert len(rows) == 3, (
        f"alice (non-admin) must see exactly her own 3 rows, "
        f"got {len(rows)}: {rows!r}")
    scheduled_by = [r.get("scheduled_by") for r in rows]
    assert all(s == "alice@example.com" for s in scheduled_by), (
        f"every row must be scheduled_by=alice, got {scheduled_by!r}")
    # The web-trigger row is among hers (visible to its owner).
    assert "web" in [r.get("trigger") for r in rows], (
        f"alice's trigger='web' row must be visible to her: {rows!r}")


# --- 10. admin sees all rows -----------------------------------------------------


def test_audit_recent_admin_sees_all(web_app):
    """Admin GET /api/audit/recent → ALL users' rows (the documented admin
    privilege, NFR-16): alice's, bob's, and system rows all included.

    RED: the route is unimplemented (T014) → the scaffold's clean JSON 404.
    """
    _app, db, host, port, tokens = web_app
    _seed_audit_row(db, "2026-01-01T00:00:00+00:00", "alice@example.com",
                    "manual")
    _seed_audit_row(db, "2026-01-02T00:00:00+00:00", "alice@example.com",
                    "web")
    _seed_audit_row(db, "2026-01-03T00:00:00+00:00", "bob@example.com",
                    "manual")
    _seed_audit_row(db, "2026-01-04T00:00:00+00:00", "system", "schedule")

    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent",
        headers={"Authorization": f"Bearer {tokens['admin@example.com']}"})
    assert code == 200, (
        f"GET /api/audit/recent (admin) expected 200, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    rows = parsed.get("rows")
    assert isinstance(rows, list), f"'rows' must be a list: {parsed!r}"
    assert len(rows) == 4, (
        f"admin must see all 4 rows, got {len(rows)}: {rows!r}")
    scheduled_by = {r.get("scheduled_by") for r in rows}
    assert scheduled_by == {"alice@example.com", "bob@example.com",
                            "system"}, (
        f"admin must see every user's rows, got {scheduled_by!r}")


# --- 11. audit shape + ordering + limit ------------------------------------------


def test_audit_recent_audit_shape(web_app):
    """Rows in the 001/002/004 audit shape, most recent first, with the
    ``limit`` default (10) and cap (100) honoured.

    - Each row: ``run_id``, ``started_at``, ``completed_at``, ``status``,
      ``trigger``, ``scheduled_by``, per-source counts (``fs`` = 3).
    - Most recent first (seeded ts strictly descending).
    - Seed 15 rows, no ``?limit=`` → exactly the default-10 most recent.
    - ``?limit=3`` → exactly the 3 most recent.

    RED: the route is unimplemented (T014) → the scaffold's clean JSON 404.
    """
    _app, db, host, port, tokens = web_app
    base = "2026-02-0%dT00:00:00+00:00"
    for i in range(1, 16):  # 15 rows, ts strictly ascending
        _seed_audit_row(
            db, base % i, "alice@example.com", "manual",
            per_source_counts={"fs": 3},
        )

    # (a) Default limit: 10 of the 15 rows, most recent first.
    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent",
        headers={"Authorization": f"Bearer {tokens['alice@example.com']}"})
    assert code == 200, (
        f"GET /api/audit/recent expected 200, got {code}: {raw[:300]!r}"
    )
    rows = (parsed or {}).get("rows")
    assert isinstance(rows, list), f"'rows' must be a list: {parsed!r}"
    assert len(rows) == 10, (
        f"default limit must be 10 rows (of 15 seeded), got {len(rows)}")
    # Most recent first: ts day 15 down to day 6.
    assert [r["run_id"] for r in rows[:0]] == []  # shape check only
    expected_ts_desc = [f"2026-02-0{d}T00:00:00+00:00" for d in
                        [15, 14, 13, 12, 11, 10, 9, 8, 7, 6]]
    assert [r["started_at"] for r in rows] == expected_ts_desc, (
        f"rows must be most-recent-first with the default limit; got "
        f"{[r.get('started_at') for r in rows]!r}")
    # (b) Per-row shape: the 001/002/004 audit record fields.
    for row in rows:
        assert set(row) >= {"run_id", "started_at", "completed_at", "status",
                            "trigger", "scheduled_by"}, (
            f"each row must carry the audit shape, got {set(row)}: {row!r}")
        assert row["status"] == "ok", f"got {row!r}"
        assert row["trigger"] == "manual", f"got {row!r}"
        assert row["scheduled_by"] == "alice@example.com", f"got {row!r}"
        # Per-source counts: JSON-encoded in the table, decoded for the UI.
        counts = row.get("per_source_counts")
        assert counts == {"fs": 3}, (
            f"per-source counts must be the seeded {{'fs': 3}}, got "
            f"{counts!r} (type {type(counts).__name__})")
    # (c) Explicit limit: 3 most recent.
    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent?limit=3",
        headers={"Authorization": f"Bearer {tokens['alice@example.com']}"})
    assert code == 200, f"got {code}: {raw[:300]!r}"
    rows3 = (parsed or {}).get("rows")
    assert isinstance(rows3, list) and len(rows3) == 3, (
        f"limit=3 must yield exactly 3 rows, got "
        f"{len(rows3) if isinstance(rows3, list) else rows3!r}")
    assert [r["started_at"] for r in rows3] == [
        "2026-02-015T00:00:00+00:00",
        "2026-02-014T00:00:00+00:00",
        "2026-02-013T00:00:00+00:00"], (
        f"limit=3 must be the 3 most recent, got "
        f"{[r.get('started_at') for r in rows3]!r}")
    # (d) Cap: ?limit=500 is clamped to 100 — with only 15 rows the
    # response is all 15 (the cap is the upper bound, not the target).
    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent?limit=500",
        headers={"Authorization": f"Bearer {tokens['alice@example.com']}"})
    assert code == 200, f"got {code}: {raw[:300]!r}"
    rows500 = (parsed or {}).get("rows")
    assert isinstance(rows500, list) and len(rows500) == 15, (
        f"?limit=500 must be capped at 100 (all 15 seeded rows returned), "
        f"got {len(rows500) if isinstance(rows500, list) else rows500!r}")


# --- 12. web-triggered run visible immediately ------------------------------------


def test_audit_recent_web_trigger_visible(web_app, monkeypatch):
    """A run just triggered via POST /api/ingest/run is visible immediately
    in GET /api/audit/recent (for its owner).

    Both calls ride the live server loop; ``run_pipeline`` is monkeypatched
    to capture kwargs and return a fake summary, and the 'web'-trigger audit
    row is written the way the GREEN handler must: through the pipeline path
    (R3) — i.e. the test asserts the row exists with trigger='web' and
    scheduled_by=the caller, then that /api/audit/recent surfaces it.

    RED: /api/ingest/run is unimplemented (T012) → the first POST is a
    clean JSON 404, so the test fails on the 200 assertion.
    """
    app, db, host, port, tokens = web_app
    caller = "alice@example.com"
    token = tokens[caller]

    captured: dict = {}

    def fake_run_pipeline(cfg, db, qdrant, *args, **kwargs):
        captured["kwargs"] = kwargs
        from digital_twins.ingest.pipeline import RunSummary
        from digital_twins.state.models import start_audit_run, finish_audit_run
        run_id = f"run-web-{uuid.uuid4().hex[:8]}"
        start_audit_run(db, run_id, trigger=kwargs.get("trigger", "web"),
                        scheduled_by=kwargs.get("scheduled_by", caller))
        finish_audit_run(db, run_id, "ok", {"fs": 1})
        return RunSummary(run_id=run_id, counts={"fs": 1}, points=1,
                          status="ok")

    monkeypatch.setattr(
        "digital_twins.ingest.pipeline.run_pipeline", fake_run_pipeline)

    # Trigger a web run (POST — the ingest surface).
    conn = http.client.HTTPConnection(host, port, timeout=5.0)
    try:
        conn.request(
            "POST", "/api/ingest/run",
            body=json.dumps({"source": "fs"}),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        trigger_raw = resp.read()
        trigger_code = resp.status
    finally:
        conn.close()
    assert trigger_code == 200, (
        f"POST /api/ingest/run expected 200, got {trigger_code}: "
        f"{trigger_raw[:300]!r}"
    )
    trigger_parsed = json.loads(trigger_raw.decode("utf-8"))
    run_id = trigger_parsed.get("run_id")
    assert run_id, f"trigger response must carry run_id: {trigger_parsed!r}"

    # The audit row exists, trigger='web', scheduled_by=caller.
    row = db.execute(
        "SELECT trigger, scheduled_by FROM audit_runs WHERE run_id=?",
        (run_id,)).fetchone()
    assert row is not None, (
        f"the web-triggered run must have an audit_runs row, "
        f"got none for {run_id!r}: "
        f"{db.execute('SELECT * FROM audit_runs').fetchall()!r}")
    assert row[0] == "web", f"trigger must be 'web', got {row!r}"
    assert row[1] == caller, f"scheduled_by must be the caller, got {row!r}"

    # It is visible immediately in /api/audit/recent for the owner.
    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent?limit=10",
        headers={"Authorization": f"Bearer {token}"})
    assert code == 200, (
        f"GET /api/audit/recent expected 200, got {code}: {raw[:300]!r}"
    )
    rows = (parsed or {}).get("rows")
    assert isinstance(rows, list), f"'rows' must be a list: {parsed!r}"
    assert any(r.get("run_id") == run_id for r in rows), (
        f"the just-triggered run ({run_id}) must appear in "
        f"/api/audit/recent immediately; rows: {rows!r}")


# --- 13. no/invalid token → 401 ---------------------------------------------------


def test_audit_recent_no_token_401(web_app):
    """GET /api/audit/recent with no token → 401; with an invalid token →
    401 (fail-closed gate, both shapes pinned).

    RED/GREEN: the gate is T004's (already green) — the 401 holds now and
    after T014 lands; this pins the auth boundary of the new route.
    """
    _app, _db, host, port, _tokens = web_app

    # No token at all.
    code, parsed, raw = _http_get(host, port, "/api/audit/recent")
    assert code == 401, (
        f"GET /api/audit/recent without a token expected 401, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")

    # Invalid (unforgeable-looking but unverified) token.
    code, parsed, raw = _http_get(
        host, port, "/api/audit/recent",
        headers={"Authorization": "Bearer deadbeef" * 8})
    assert code == 401, (
        f"GET /api/audit/recent with an invalid token expected 401, "
        f"got {code}: {raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")
