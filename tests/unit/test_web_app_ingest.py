"""T011 [006-web-app] RED: /api/ingest/run — trigger=web, role gate, dedup parity.

These tests pin ``POST /api/ingest/run`` (FR-007..FR-009, SC-002/SC-004)
against the WebApp scaffold.  The handler does NOT exist yet (T012 lands
it): the T004 scaffold's bearer gate passes for a valid session token and
its ``_dispatch_rest`` falls through to a clean JSON 404 for the
unimplemented route.  So every behavioural test in this file fails with
``404`` — not a fixture error, not an auth error — which is the honest RED
the constitution demands.  The no-token test (test 8) is the auth-boundary
guard and is already green from T004.

Harness pattern mirrors ``tests/unit/test_web_app_kb.py``: migrated v3 state
DB in ``tmp_path``, ``build_web_app`` on 127.0.0.1:0, ``serve``, requests
driven with ``http.client``.

Qdrant is never live (spec C-5): the dedup-parity test seeds an in-process
fake client onto ``app.qdrant_client`` and drives the REAL
``run_pipeline`` against it (the direct-call leg of the parity check), so
the one-record invariant is asserted on actual pipeline behaviour.
"""

from __future__ import annotations

import http.client
import json
import socket
import time

import pytest

from digital_twins.accounts import create_account
from digital_twins.auth import create_session
from digital_twins.ingest import pipeline as pipeline_mod
from digital_twins.ingest.pipeline import RunSummary
from digital_twins.state import db as state_db

_READY_TIMEOUT_S = 5.0

# --- fixture -----------------------------------------------------------------


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


@pytest.fixture
def web_app(tmp_path):
    """Build + serve the 006 WebApp on 127.0.0.1:0 with a v3 state DB.

    Mirrors the fixture in ``test_web_app_kb.py``: one account
    (``web-user@example.com`` — the first row, so role admin per R7) with a
    live session token.  Yields ``(app, db, host, port, session_token)``.
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


# --- helpers ------------------------------------------------------------------


def _audit_rows(db):
    """All ``audit_runs`` rows as dicts."""
    rows = db.execute(
        "SELECT run_id, started_at, completed_at, status, trigger, "
        "scheduled_by, per_source_counts FROM audit_runs"
    ).fetchall()
    return [dict(zip(
        ("run_id", "started_at", "completed_at", "status", "trigger",
         "scheduled_by", "per_source_counts"), row)) for row in rows]


def _audit_row(db, run_id: str) -> dict:
    row = db.execute(
        "SELECT run_id, started_at, completed_at, status, trigger, "
        "scheduled_by, per_source_counts FROM audit_runs WHERE run_id=?",
        (run_id,)).fetchone()
    assert row is not None, f"audit_runs row for run_id={run_id!r} missing"
    return dict(zip(
        ("run_id", "started_at", "completed_at", "status", "trigger",
         "scheduled_by", "per_source_counts"), row))


class _InMemoryQdrant:
    """Minimal in-memory Qdrant stand-in for a real ``run_pipeline`` run.

    Supports exactly the surface ``run_pipeline`` exercises
    (``collection_exists`` / ``create_collection`` / ``get_collection`` /
    ``upsert`` / ``count``) with a fixed 4-dim vector space.  Dedup is
    content-level: upsert replaces any point whose id already exists,
    mirroring the deterministic point-id invariant (NFR-1).
    """

    def __init__(self, dim=4):
        self.dim = dim
        self.collections: dict[str, dict] = {}
        self.points: list = []

    def collection_exists(self, name):
        return name in self.collections

    def create_collection(self, name, vectors_config=None, **_kw):
        size = getattr(vectors_config, "size", None)
        self.collections[name] = {"vectors": {"size": size}}

    def get_collection(self, name):
        class _Vectors:
            size = self.collections[name]["vectors"]["size"]
        class _Params:
            vectors = _Vectors()
        class _Config:
            params = _Params()
        class _Info:
            config = _Config()
        return _Info()

    def upsert(self, collection, points=None, wait=None, **_kw):
        by_id = {p.id: p for p in self.points}
        for p in points:
            by_id[p.id] = p  # deterministic-id upsert: replace, don't append
        self.points = list(by_id.values())

    def count(self, collection, count_filter=None, exact=True, **_kw):
        class _CountResult:
            count = len(self.points)
        return _CountResult()


# --- 1. trigger=web + scheduled_by + audit row + run summary ------------------


def test_ingest_run_trigger_web_scheduled_by(web_app, monkeypatch, tmp_path):
    """POST /api/ingest/run {source:'fs'} → 200 run summary; run_pipeline is
    called with trigger='web' and scheduled_by=<caller-email>; the
    audit_runs row is written with trigger='web'.

    The fs source is enabled in the app config and its prerequisite (the
    extra.dir path) exists, so the request reaches the pipeline.  The
    pipeline call itself is monkeypatched to capture kwargs and return a
    fake run summary — the assertions pin the *boundary* (the hand-off to
    run_pipeline), not pipeline internals.

    RED: the route is unimplemented (T012) → the scaffold's clean JSON 404.
    """
    app, db, host, port, session_token = web_app
    caller = "web-user@example.com"

    # Enabled source with prerequisites present: point sources.fs.extra.dir
    # at a real directory.
    fs_dir = tmp_path / "fs-src"
    fs_dir.mkdir()
    app.config["sources"]["fs"] = {
        "enabled": True,
        "extra": {"dir": str(fs_dir)},
    }

    captured: dict = {}

    def fake_run_pipeline(cfg, db, qdrant, *args, **kwargs):
        captured["kwargs"] = kwargs
        captured["cfg"] = cfg
        captured["db"] = db
        # Simulate the real pipeline's audit-row write (R3: the pipeline
        # writes the row, not the handler).
        from digital_twins.state.models import start_audit_run, finish_audit_run
        start_audit_run(db, "run-web-1",
                        trigger=kwargs.get("trigger", "web"),
                        scheduled_by=kwargs.get("scheduled_by", "system"))
        finish_audit_run(db, "run-web-1", "ok", {"fs": 2})
        return RunSummary(run_id="run-web-1", counts={"fs": 2},
                          points=3, status="ok")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake_run_pipeline)

    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "fs"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"POST /api/ingest/run expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    # Response is the run summary.
    assert parsed.get("run_id") == "run-web-1", (
        f"response must carry the pipeline run_id, got {parsed!r}")
    assert parsed.get("status") == "ok", f"got {parsed!r}"
    assert parsed.get("counts") == {"fs": 2}, f"got {parsed!r}"
    assert parsed.get("points") == 3, f"got {parsed!r}"

    # run_pipeline was called with trigger='web' and scheduled_by=<caller>.
    assert captured, (
        "run_pipeline was never called — the handler did not reach the "
        "pipeline hand-off")
    kwargs = captured["kwargs"]
    assert kwargs.get("trigger") == "web", (
        f"run_pipeline must be called with trigger='web', "
        f"got trigger={kwargs.get('trigger')!r}")
    assert kwargs.get("scheduled_by") == caller, (
        f"run_pipeline must be called with scheduled_by=<caller-email> "
        f"({caller!r}), got {kwargs.get('scheduled_by')!r}")

    # The audit_runs row is written with trigger='web' (same pipeline path,
    # R3 — the pipeline writes the row; T012 must not bypass it).
    rows = _audit_rows(db)
    web_rows = [r for r in rows if r["trigger"] == "web"]
    assert web_rows, (
        f"an audit_runs row with trigger='web' must be written, "
        f"audit table: {rows!r}")
    row = web_rows[0]
    assert row["run_id"] == "run-web-1", (
        f"the trigger='web' audit row must reference the run, got {row!r}")
    assert row["scheduled_by"] == caller, (
        f"the trigger='web' audit row must carry scheduled_by=<caller>, "
        f"got {row!r}")
    assert row["status"] in ("ok", "partial", "failed"), f"got {row!r}"


# --- 2. dedup parity (NFR-1 / NFR-14) -----------------------------------------


def test_ingest_run_dedup_parity(web_app):
    """The same content ingested via ``run_pipeline`` (direct call — the
    ``run --once`` equivalent) and then via ``/api/ingest/run`` for the same
    source + window yields ONE point, not two (NFR-1/NFR-14).

    Leg 1 (direct) runs the REAL ``run_pipeline`` against an in-memory
    Qdrant stand-in: the deterministic point id dedups nothing yet, so one
    point lands.  Leg 2 (web) must go through the same pipeline path with
    the SAME point id, so the Qdrant point count is unchanged after the web
    run (one record, not N).

    RED: /api/ingest/run is unimplemented (T012) → clean JSON 404; leg 2
    never runs, so the count assertion is vacuous only because the 404
    assertion already fails the test.
    """
    app, db, host, port, session_token = web_app
    caller = "web-user@example.com"

    # A real fs source: the directory must exist (prerequisite) and hold
    # one file whose content is the "same content" both legs ingest.
    # The dir lives under the app's state dir (tmp_path) — host-neutral.
    fs_dir = _fs_dir_for(app)
    (fs_dir / "one.txt").write_text("parity content\n", encoding="utf-8")

    app.config["sources"]["fs"] = {
        "enabled": True,
        "extra": {"dir": str(fs_dir)},
    }

    fake = _InMemoryQdrant()
    app.qdrant_client = fake

    # A minimal fake embedder: return a fixed 4-dim vector for every text.
    def fake_embed(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    # Leg 1: the same content via run_pipeline directly (run --once parity).
    summary = pipeline_mod.run_pipeline(
        app.config, app.db, qdrant=fake, embedder=fake_embed,
        source_names=["fs"],
        trigger="manual", scheduled_by="system", owner=None,
    )
    assert summary.status == "ok", f"leg 1 pipeline run failed: {summary!r}"
    assert len(fake.points) == 1, (
        f"leg 1: expected exactly 1 point after the direct pipeline run, "
        f"got {len(fake.points)}: {[p.id for p in fake.points]!r}")

    # Leg 2: the SAME source + window via the web surface (high-water cursor
    # is in the same DB, content unchanged → the pipeline must dedup to the
    # same point id, not a new one).
    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "fs"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"POST /api/ingest/run (dedup parity) expected 200, got {code}: "
        f"{raw[:300]!r}"
    )
    # The invariant: one record, not N.
    assert len(fake.points) == 1, (
        f"dedup parity violated: the web-triggered run must not duplicate "
        f"the content-level record — expected 1 point, "
        f"got {len(fake.points)}: {[p.id for p in fake.points]!r}")


def _fs_dir_for(app):
    """A per-app fs source directory (host-neutral, under the state dir)."""
    from pathlib import Path
    root = Path(app.config.get("state_dir", "."))
    d = root / "web-fs-src"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- 3. reader role → 403, no audit row ----------------------------------------


def test_ingest_run_reader_role_403(web_app, monkeypatch):
    """A reader-role caller gets 403 {code:'permission_denied'} naming the
    missing ``trigger_run`` capability, and NO audit row is written on
    refusal (the capability check happens before any pipeline work).

    RED: the route is unimplemented (T012) → clean JSON 404.
    """
    app, db, host, port, session_token = web_app
    monkeypatch.setattr(
        "digital_twins.accounts.get_role", lambda _db, _email: "reader")

    pipeline_touched = {"flag": False}

    def guard_pipeline(*a, **kw):
        pipeline_touched["flag"] = True
        return RunSummary(run_id="should-not-happen")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", guard_pipeline)

    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "fs"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 403, (
        f"reader-role POST /api/ingest/run expected 403, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert parsed.get("code") == "permission_denied", (
        f"403 body must carry code='permission_denied', got {parsed!r}")
    # The message names the missing capability.
    assert "trigger_run" in str(parsed.get("message", "")), (
        f"403 message must name the missing 'trigger_run' capability, "
        f"got {parsed!r}")
    # No pipeline work on refusal.
    assert not pipeline_touched["flag"], (
        "the capability gate must run BEFORE any pipeline work")
    # No audit row on refusal.
    assert _audit_rows(db) == [], (
        f"no audit_runs row may be written on a 403 refusal, "
        f"got {_audit_rows(db)!r}")


# --- 4. disabled source → 400 ---------------------------------------------------


def test_ingest_run_disabled_source_400(web_app, monkeypatch):
    """{source:'foo'} with foo present in config but disabled → 400
    {"error": "source 'foo' is not enabled"}.

    RED: the route is unimplemented (T012) → clean JSON 404.
    """
    app, db, host, port, session_token = web_app
    app.config["sources"]["foo"] = {"enabled": False}

    pipeline_touched = {"flag": False}

    def guard_pipeline(*a, **kw):
        pipeline_touched["flag"] = True
        return RunSummary(run_id="should-not-happen")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", guard_pipeline)

    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "foo"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 400, (
        f"disabled source expected 400, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert parsed.get("error") == "source 'foo' is not enabled", (
        f"400 body must be the contract error, got {parsed!r}")
    assert not pipeline_touched["flag"], (
        "validation must reject BEFORE any pipeline work")
    assert _audit_rows(db) == [], (
        f"no audit row may be written for a rejected source, "
        f"got {_audit_rows(db)!r}")


# --- 5. unknown source → 400 ----------------------------------------------------


def test_ingest_run_unknown_source_400(web_app, monkeypatch):
    """{source:'bar'} with bar not a known source at all → 400
    {"error": "unknown source 'bar'"} (mirrors run --once UnknownSourceError).

    RED: the route is unimplemented (T012) → clean JSON 404.
    """
    app, db, host, port, session_token = web_app

    pipeline_touched = {"flag": False}

    def guard_pipeline(*a, **kw):
        pipeline_touched["flag"] = True
        return RunSummary(run_id="should-not-happen")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", guard_pipeline)

    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "bar"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 400, (
        f"unknown source expected 400, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert parsed.get("error") == "unknown source 'bar'", (
        f"400 body must be the contract error, got {parsed!r}")
    assert not pipeline_touched["flag"], (
        "validation must reject BEFORE any pipeline work")
    assert _audit_rows(db) == [], (
        f"no audit row may be written for an unknown source, "
        f"got {_audit_rows(db)!r}")


# --- 6. 'all' / {} with none enabled → 400 ---------------------------------------


def test_ingest_run_no_enabled_sources_400(web_app, monkeypatch):
    """'all' (or {}) with NO sources enabled → 400
    {"error": "no sources enabled"}.  Both body shapes are pinned.

    RED: the route is unimplemented (T012) → clean JSON 404.
    """
    app, db, host, port, session_token = web_app
    # None enabled: the fs source is known but off.
    app.config["sources"]["fs"] = {"enabled": False}

    pipeline_touched = {"flag": False}

    def guard_pipeline(*a, **kw):
        pipeline_touched["flag"] = True
        return RunSummary(run_id="should-not-happen")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", guard_pipeline)

    for body in ({"source": "all"}, {}):
        code, parsed, raw = _http_post(
            host, port, "/api/ingest/run", body,
            headers={"Authorization": f"Bearer {session_token}"})
        assert code == 400, (
            f"{body!r} with no enabled sources expected 400, got {code}: "
            f"{raw[:300]!r}"
        )
        assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
        assert parsed.get("error") == "no sources enabled", (
            f"400 body must be the contract error, got {parsed!r}")
    assert not pipeline_touched["flag"], (
        "validation must reject BEFORE any pipeline work")
    assert _audit_rows(db) == [], (
        f"no audit row may be written when nothing is enabled, "
        f"got {_audit_rows(db)!r}")


# --- 7. enabled source missing prerequisites → 409/500 + failed audit row ------


def test_ingest_run_missing_prerequisites_409(web_app):
    """An enabled source whose prerequisite is missing → 409/500 naming the
    missing prerequisite(s), AND a 'failed' audit row is written (the
    pipeline's fail-fast path, constitution IV).

    The fs source's dir does not exist → run_pipeline raises
    PrerequisiteError and audits the run as 'failed' (the REAL pipeline is
    driven here: no monkeypatch — the audit row must come from the same
    code path the GREEN handler will use).

    RED: the route is unimplemented (T012) → clean JSON 404.
    """
    app, db, host, port, session_token = web_app
    # Enabled, but extra.dir points at a path that does not exist.
    app.config["sources"]["fs"] = {
        "enabled": True,
        "extra": {"dir": str(_fs_dir_for(app) / "does-not-exist")},
    }
    app.qdrant_client = _InMemoryQdrant()  # a live client; the check is pre-flight

    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "fs"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code in (409, 500), (
        f"missing prerequisites expected 409/500, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    err = str(parsed.get("error", ""))
    assert "prerequisite" in err.lower() or "does not exist" in err, (
        f"the error must name the missing prerequisite(s), got {parsed!r}")
    # A 'failed' audit row was written (Constitution V: a row for every run,
    # regardless of outcome).
    failed = [r for r in _audit_rows(db) if r["status"] == "failed"]
    assert failed, (
        f"a 'failed' audit_runs row must be written on prerequisite "
        f"failure, audit table: {_audit_rows(db)!r}")


# --- 8. no token → 401 ----------------------------------------------------------


def test_ingest_run_no_token_401(web_app):
    """POST /api/ingest/run without a Bearer token → 401 (fail-closed gate).

    The bearer gate rejects BEFORE any handler runs, so the status is 401
    even though the route is unimplemented.

    RED/GREEN: the gate is T004's (already green) — the 401 holds now and
    after T012 lands; this pins the auth boundary of the new route.
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw = _http_post(host, port, "/api/ingest/run", {})
    assert code == 401, (
        f"POST /api/ingest/run without a token expected 401, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")
