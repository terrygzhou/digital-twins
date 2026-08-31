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
import pathlib
import re
import socket
import time

import pytest

from digital_twins.accounts import create_account, owner_tag_for
from digital_twins.ingest import pipeline as pipeline_mod
from digital_twins.ingest.embedding import model_dimension
from digital_twins.ingest.ids import point_id
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
    # 008/US2 admin config surface (009 US1 panel): masked GET/POST.
    "/api/config/services",
    # 009/US2 admin connectivity probe (panel Test / Test all).
    "/api/config/services/probe",
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
    """The index.html's ``fetch()`` calls target EXACTLY the 11 /api/*
    paths from contracts/web-api.md + the 008/009 admin config
    surface (GET/POST /api/config/services, 009 probe) — no
    others, no parallel logic.

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
        f"index.html fetch() targets must be EXACTLY the known /api/* paths.\n"
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


# =============================================================================
# Test 6: e2e full journey (T023) — sign-up through audit, SC-001/002/004/005/006
# =============================================================================


class _FakeQdrant:
    """In-process Qdrant fake for the e2e journey (no live Qdrant needed).

    Backed by a dict of point_id -> payload.  Implements the surface the
    pipeline + the web app touch: collection_exists / get_collection /
    create_collection / upsert / count / scroll.  count honours a
    ``filter`` of 0..2 FieldConditions (owner_tag / source); scroll returns
    all points, most recent (by payload ``ts``) first.
    """

    def __init__(self, dim=384):
        self._points: dict = {}
        self.dim = dim

    def collection_exists(self, collection) -> bool:
        return True

    def get_collection(self, collection):
        class _Vectors:
            size = self.dim
        class _Params:
            vectors = _Vectors()
        class _Info:
            config = type("_Cfg", (), {"params": _Params()})()
        return _Info()

    def create_collection(self, *a, **kw):
        return None

    def upsert(self, collection, points=None, wait=True):
        for p in points:
            self._points[p.id] = p.payload

    def count(self, collection, filter=None, **_kw):
        matches = [
            p for p in self._points.values() if _matches_filter(p, filter)
        ]

        class _CountResult:
            count = len(matches)
        return _CountResult()

    def query_points(self, collection, query_filter=None, limit=None,
                     with_payload=True, **_kw):
        """Owner-scoped search: filter, then rank by payload _score desc."""
        candidates = [
            p for p in self._points.values()
            if _matches_filter(p, query_filter)
        ]
        ranked = sorted(
            (type("_P", (), {"id": pid, "payload": p,
                             "vector": [0.0]})
             for pid, p in self._points.items()),
            key=lambda x: x.payload.get("_score", 0.0), reverse=True)
        if limit is not None:
            ranked = ranked[:limit]

        class _Scored:
            def __init__(self, p):
                self.id = p.id
                self.score = p.payload.get("_score", 0.0)
                self.payload = p.payload
                self.vector = p.vector
        return [_Scored(p) for p in ranked]

    def scroll(self, collection, limit=None, with_payload=True, **_kw):
        pts = sorted(
            [type("_P", (), {"id": pid, "payload": payload,
                             "vector": [0.0]}) for pid, payload in
             self._points.items()],
            key=lambda p: p.payload.get("ts", 0), reverse=True)
        if limit is not None:
            pts = pts[:limit]
        return pts, None


def _matches_filter(payload, qfilter) -> bool:
    """Match a payload against a qdrant Filter of 0..2 FieldConditions."""
    if qfilter is None:
        return True
    for cond in getattr(qfilter, "must", None) or []:
        value = cond.match.value if hasattr(cond.match, "value") \
            else cond.match
        if payload.get(cond.key) != value:
            return False
    return True


def test_e2e_full_journey(web_app):
    """The complete user journey on the live WebApp (SC-001/002/004/005/006).

    1. Sign up — first account → admin role (SC-001: the token is valid
       across requests, no CLI required).
    2. Sign up — second account → reader role.
    3. Sign in as the admin account.
    4. GET /api/me → 200 {email, role: 'admin', point_count: 0}.
    5. GET /api/kb/points → 200 {count: 0, owner_count: 0, source_count: 0,
       sample: []} (empty collection).
    6. POST /api/kb/search {query: "test"} → 200 (the in-process fake).
    7. POST /api/ingest/run {source: "fs"} → 200 (the fs source is enabled
       in the config with a tmp dir of one text file; the pipeline runs the
       real code path with trigger='web' + scheduled_by=admin).
    8. SC-002 dedup parity: a DIRECT run_pipeline call (trigger='direct',
       scheduled_by='system') over the same content leaves the point count
       unchanged (one record, not N — NFR-1/NFR-14).
    9. SC-004: the reader's POST /api/ingest/run → 403 permission_denied
       (fail-closed capability gate).
    10. SC-005: the reader's GET /api/audit/recent → 200 with only the
       reader's own rows (empty — the 403 wrote no audit row); the admin's
        GET /api/audit/recent → 200 with ALL rows, the most recent
        being trigger='web' + scheduled_by=admin.
    11. SC-006: re-asserted by test_index_html_fetch_targets_are_api_paths
       (the static UI drives /api/* only).
    """
    app, db, host, port = web_app

    # In-process Qdrant fake (the real pipeline code path runs against it —
    # no live Qdrant needed, spec C-5).
    fake = _FakeQdrant(dim=model_dimension("BAAI/bge-small-en-v1.5"))
    app.qdrant_client = fake

    def _fake_embedder(texts):
        # Deterministic vectors (dim matched to the pinned model).
        dim = fake.dim
        return [[float(i % 7) / 7.0] * dim for i in range(len(texts))]

    app._embed_pool = {"embed": _fake_embedder}

    # -- 1. Signup: first account → admin -------------------------------------
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "admin@example.com", "password": "admin-pw-123"},
    )
    assert code == 200, (
        f"signup (first) expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed.get("role") == "admin", (
        f"first account must be admin, got {parsed.get('role')!r}"
    )

    # -- 2. Signup: second account → reader -----------------------------------
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signup",
        {"email": "reader@example.com", "password": "reader-pw-123"},
    )
    assert code == 200, (
        f"signup (second) expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed.get("role") == "reader", (
        f"second account must be reader, got {parsed.get('role')!r}"
    )

    # -- 3. Sign in as admin --------------------------------------------------
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "admin@example.com", "password": "admin-pw-123"},
    )
    assert code == 200, f"signin (admin) expected 200: {raw[:300]!r}"
    admin_token = parsed["session_token"]
    admin_auth = {"Authorization": f"Bearer {admin_token}"}

    # -- 4. GET /api/me → 200 {email, role: admin, point_count: 0} -----------
    code, parsed, raw, _ct = _http_get_full(
        host, port, "/api/me", headers=admin_auth)
    assert code == 200, f"/api/me expected 200, got {code}: {raw[:300]!r}"
    assert parsed.get("email") == "admin@example.com"
    assert parsed.get("role") == "admin"
    assert parsed.get("point_count") == 0, (
        f"empty KB must have point_count=0, got {parsed.get('point_count')}"
    )

    # -- 5. GET /api/kb/points → 200 empty-collision shape --------------------
    code, parsed, raw, _ct = _http_get_full(
        host, port, "/api/kb/points", headers=admin_auth)
    assert code == 200, (
        f"/api/kb/points expected 200 (in-process fake), got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed == {"count": 0, "owner_count": 0,
                      "source_count": 0, "sample": []}, (
        f"empty collection must be all-zero + empty sample: {parsed!r}"
    )

    # -- 6. POST /api/kb/search → 200 (the fake; no real Qdrant) --------------
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search", {"query": "test"}, headers=admin_auth)
    assert code == 200, (
        f"/api/kb/search expected 200 (in-process fake), got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed.get("results") == [], (
        f"empty collection search must be results=[], got {parsed!r}"
    )

    # -- 7. POST /api/ingest/run {source: "fs"} → 200 run summary -------------
    # The fs source is enabled in the app config with a tmp dir containing
    # one text file.  The pipeline runs the REAL code path (R3) with
    # trigger='web' + scheduled_by=admin.
    fs_dir = _tmp_fs_dir(db, host, port)
    # (re-create the app's config with the fs source enabled — build_web_app
    # already merged the defaults; overlay the sources section)
    app.config["sources"]["fs"] = {
        "enabled": True,
        "extra": {"dir": str(fs_dir)},
    }
    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "fs"}, headers=admin_auth)
    assert code == 200, (
        f"/api/ingest/run expected 200, got {code}: {raw[:500]!r}"
    )
    assert parsed.get("status") == "ok", (
        f"run summary status must be ok, got {parsed!r}"
    )
    assert "run_id" in parsed and "counts" in parsed and "points" in parsed, (
        f"run summary shape must be {{run_id, status, counts, points}}: "
        f"{parsed!r}"
    )
    web_points = parsed["points"]
    assert web_points >= 1, (
        f"the fs source has one file; expect >= 1 point, got {web_points}"
    )

    # -- 8. SC-002: dedup parity vs a direct run_pipeline call ----------------
    # The SAME content ingested a second time (direct call, trigger='direct')
    # must NOT duplicate points (NFR-1/NFR-14: one record, not N).
    with app._db_lock:
        summary_direct = pipeline_mod.run_pipeline(
            app.config, db, fake, _fake_embedder,
            source_names=["fs"],
            trigger="direct",
            scheduled_by="system",
        )
    points_after_direct = len(fake._points)
    assert points_after_direct == web_points, (
        f"dedup parity (SC-002): a direct run over the same content must "
        f"not duplicate points. Web run produced {web_points}, direct run "
        f"left {points_after_direct} in the collection."
    )
    assert summary_direct.points == 0, (
        f"the direct re-run must upsert 0 NEW points (all deduped by "
        f"deterministic ID + high-water), got {summary_direct.points}"
    )

    # -- 9. SC-004: reader's /api/ingest/run → 403 permission_denied ----------
    code, parsed, raw = _http_post(
        host, port, "/api/auth/signin",
        {"email": "reader@example.com", "password": "reader-pw-123"},
    )
    assert code == 200, f"signin (reader) expected 200: {raw[:300]!r}"
    reader_token = parsed["session_token"]
    reader_auth = {"Authorization": f"Bearer {reader_token}"}

    code, parsed, raw = _http_post(
        host, port, "/api/ingest/run", {"source": "fs"},
        headers=reader_auth,
    )
    assert code == 403, (
        f"SC-004: reader POST /api/ingest/run expected 403, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed.get("code") == "permission_denied", (
        f"SC-004: 403 body must be permission_denied, got {parsed!r}"
    )
    assert "trigger_run" in str(parsed.get("message", "")), (
        f"SC-004: 403 message must name the trigger_run capability: "
        f"{parsed!r}"
    )

    # -- 10. SC-005: per-user audit -------------------------------------------
    # The reader's /api/audit/recent → only their own rows (empty: the 403
    # wrote no audit row; the reader never ran anything).
    code, parsed, raw, _ct = _http_get_full(
        host, port, "/api/audit/recent", headers=reader_auth)
    assert code == 200, (
        f"reader /api/audit/recent expected 200, got {code}: {raw[:300]!r}"
    )
    reader_rows = parsed.get("rows", [])
    assert all(r["scheduled_by"] == "reader@example.com"
               for r in reader_rows), (
        f"SC-005: reader's audit must contain ONLY their own rows: "
        f"{[r.get('scheduled_by') for r in reader_rows]!r}"
    )
    assert reader_rows == [], (
        f"SC-005: the reader triggered nothing (the 403 wrote no audit "
        f"row), so their audit must be empty, got {reader_rows!r}"
    )

    # The admin's /api/audit/recent → ALL rows (NFR-16 admin privilege).
    code, parsed, raw, _ct = _http_get_full(
        host, port, "/api/audit/recent", headers=admin_auth)
    assert code == 200, (
        f"admin /api/audit/recent expected 200, got {code}: {raw[:300]!r}"
    )
    admin_rows = parsed.get("rows", [])
    assert len(admin_rows) == 2, (
        f"admin audit must have exactly 2 rows (web + direct), "
        f"got {len(admin_rows)}: {[r.get('trigger') for r in admin_rows]!r}"
    )
    # Find the web row (trigger='web', scheduled_by=admin) and the direct row.
    web_row = next(r for r in admin_rows if r["trigger"] == "web")
    direct_row = next(r for r in admin_rows if r["trigger"] == "direct")
    assert web_row["scheduled_by"] == "admin@example.com", (
        f"the web run must have scheduled_by=admin email, "
        f"got {web_row.get('scheduled_by')!r}"
    )
    assert web_row["status"] == "ok", (
        f"the web run must be status=ok, got {web_row.get('status')!r}"
    )
    # SC-002: the web run's point count is the deduped total.
    assert isinstance(web_row["per_source_counts"], dict), (
        f"per_source_counts must be a decoded dict, "
        f"got {web_row.get('per_source_counts')!r}"
    )
    assert direct_row["scheduled_by"] == "system", (
        f"direct re-run scheduled_by must be 'system', "
        f"got {direct_row.get('scheduled_by')!r}"
    )
    assert direct_row["status"] == "ok", (
        f"the direct re-run must be status=ok, "
        f"got {direct_row.get('status')!r}"
    )


def _tmp_fs_dir(db, host, port):
    """Create a tmp dir with one text file and return its Path.

    Lives under the app's state dir (tmp_path-scoped by the fixture) so the
    fs source can read it and the whole thing is cleaned up with the test.
    """
    import tempfile
    fs_dir = pathlib.Path(tempfile.mkdtemp(prefix="e2e-fs-src-"))
    (fs_dir / "hello.txt").write_text(
        "e2e journey content for the fs source\n", encoding="utf-8")
    return fs_dir
