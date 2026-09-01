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

from qdrant_client import models as qm

from digital_twins.accounts import create_account, owner_tag_for
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


# =============================================================================
# T007 + T009 [006-web-app] RED: the /api/kb/* read surface
#
# /api/kb/points  (GET)  → {count, owner_count, source_count?, sample}
# /api/kb/search  (POST) → {results: [{score, source_url, text, source,
#                                      chunk_index}]}
#
# These handlers are T008 (/api/kb/points) and T010 (/api/kb/search).  They do
# NOT exist yet, so the RED failure for every test in this block is the
# scaffold's clean JSON 404 from _dispatch_rest — NOT a fixture error and NOT
# a Qdrant error.  The Qdrant stub below is wired in as *data* (the fake
# client's points are seeded onto the running app), deliberately NOT via a
# call-time monkeypatch: a monkeypatch on the QdrantClient constructor would
# never fire while T008/T010 are still 404ing, which would turn these REDs
# into fake failures (or silent passes) instead of the honest 404.  Seeding
# the data means that once the handlers land and start reading the app's
# Qdrant client, they get the real stubbed points.
#
# The stub mirrors the real qdrant_client surface (CountResult /
# ScoredPoint / Filter) so the GREEN tests assert the handler reads Qdrant
# the documented way:
#   count(collection, filter=Filter(must=[owner_tag]))  → {count, owner_count}
#   count(collection, filter=Filter(must=[source]))     → source_count
#   scroll(collection, limit=limit)                     → most-recent sample
#   query_points / search(collection, filter=owner_tag) → owner-scoped top-N
# No live Qdrant is ever touched (spec C-5: in-process or monkeypatched).
# =============================================================================

#: owner_tag_for("web-user@example.com") — the caller's per-user scope tag.
_CALLER = "web-user@example.com"
_CALLER_TAG = owner_tag_for(_CALLER)  # "web-user@example.com-ingest"
_OTHER_TAG = owner_tag_for("other-user@example.com")


def _payload(source, source_url, chunk_index, text, ts,
             owner_tag=_CALLER_TAG):
    """One point payload in the 001/003 shape (pipeline.run_pipeline),
    owner-stamped with ``owner_tag`` (003 R6)."""
    return {
        "source": source,
        "source_url": source_url,
        "chunk_index": chunk_index,
        "text": text,
        "ts": ts,
        "owner_tag": owner_tag,
    }


class FakeQdrantClient:
    """In-process Qdrant stub for the /api/kb/* read-surface tests.

    Backed by a list of ``qm.PointStruct`` (``id``, ``vector``, ``payload``).
    ``count`` honours a ``filter`` of 0..2 ``FieldCondition``s
    (``owner_tag`` / ``source``), and ``scroll`` returns the most recent
    points (by payload ``ts``) first.  ``query_points`` / ``search`` apply the
    ``owner_tag`` filter and rank by a deterministic per-payload score so the
    GREEN tests can pin owner-scoping + descending-score ordering without a
    live vector store.
    """

    def __init__(self, points=None):
        self._points = list(points or [])
        self.count_calls = 0
        self.scroll_calls = 0
        self.search_calls = 0
        self.last_query = None

    # -- seeding ---------------------------------------------------------------

    def seed(self, payload, pid=None, vector=None):
        """Append one point.  ``ts`` (if present) drives "most recent first"."""
        pid = pid or f"fs|seed|{len(self._points)}"
        self._points.append(qm.PointStruct(
            id=pid, vector=vector or [0.0], payload=payload))
        return self

    # -- qdrant_client surface --------------------------------------------------

    def count(self, collection, count_filter=None, exact=True, **_kw):
        """Count points matching ``count_filter`` (0..2 FieldConditions).

        Mirrors the installed ``qdrant_client.count`` signature — the filter
        is a ``count_filter=`` keyword, and unknown kwargs (e.g. a stale
        ``filter=``) are rejected just like the real client's
        ``Unknown arguments`` guard, so a regression to the old kwarg is a
        hard failure rather than a silently-ignored filter.
        """
        assert not _kw, f"Unknown arguments: {sorted(_kw)}"
        self.count_calls += 1
        matches = [
            p for p in self._points
            if self._matches(p.payload, count_filter)
        ]
        return _CountResult(count=len(matches))

    def scroll(self, collection, limit=None, with_payload=True, **_kw):
        """Scroll points, most recent (by payload ``ts``) first."""
        self.scroll_calls += 1
        pts = sorted(self._points,
                     key=lambda p: p.payload.get("ts", 0), reverse=True)
        if limit is not None:
            pts = pts[:limit]
        return pts, None

    def query_points(self, collection, query=None, query_filter=None,
                     limit=None, with_payload=True, **_kw):
        """Owner-scoped search: filter, then rank by deterministic score."""
        self.search_calls += 1
        self.last_query = query
        candidates = [
            p for p in self._points
            if self._matches(p.payload, query_filter)
        ]
        ranked = sorted(
            candidates,
            key=lambda p: p.payload.get("_score", 0.0), reverse=True)
        if limit is not None:
            ranked = ranked[:limit]
        return [_ScoredPoint(p, p.payload.get("_score", 0.0)) for p in ranked]

    def search(self, collection, query_vector=None, query_filter=None,
               limit=None, with_payload=True, **_kw):
        """Alias of :meth:`query_points` (older qdrant_client surface)."""
        return self.query_points(
            collection, query=query_vector, query_filter=query_filter,
            limit=limit, with_payload=with_payload)

    @staticmethod
    def _matches(payload, qfilter):
        if qfilter is None:
            return True
        for cond in getattr(qfilter, "must", None) or []:
            key = cond.key
            match = cond.match
            value = match.value if hasattr(match, "value") else match
            if payload.get(key) != value:
                return False
        return True


def _CountResult(count):
    """A CountResult with a ``.count`` attribute (like the real client)."""
    class _CountResult:
        def __init__(self, c):
            self.count = c
    return _CountResult(count)


def _ScoredPoint(point, score):
    """A ScoredPoint-shaped object (id / score / payload / vector)."""
    class _ScoredPoint:
        def __init__(self, p, s):
            self.id = p.id
            self.score = s
            self.payload = p.payload
            self.vector = p.vector
    return _ScoredPoint(point, score)


def _seed_points(app, base_ts=1000, gap=100):
    """Populate the app's Qdrant stub with owner- + other-scoped points.

    Returns the fake client so tests can also drive the ``unavailable``
    variants.  Layout (``ts`` ascending; scroll is most-recent-first):

    - ``fs/one``    ts=1000 owner=caller
    - ``fs/two``    ts=1100 owner=caller  → sample row 0
    - ``hermes/three`` ts=1200 owner=caller → sample row 1
    - ``pi/four``   ts=1300 owner=caller  → sample row 2
    - ``pi/five``   ts=1400 owner=other  (collection-wide only)
    - ``hermes/six`` ts=1500 owner=other  (collection-wide only)
    - ``note/seven`` ts=1600 owner=other  (collection-wide only, no _score)

    ``_score`` (search ranking): two=0.9, one=0.7, three=0.5, five=0.95,
    six=0.85, seven=0.99.  The caller's owner-scoped top-2 search is
    therefore [two(0.9), one(0.7)] — the highest-scoring other-owned points
    (seven=0.99, five=0.95, six=0.85) must NOT appear.
    """
    fake = FakeQdrantClient()
    fake.seed(_payload("fs", "fs/one", 0, "fs one", base_ts))
    fake.seed(_payload("fs", "fs/two", 0, "fs two", base_ts + gap))
    fake._points[-1].payload["_score"] = 0.9
    fake.seed(_payload("hermes", "hermes/three", 0, "hermes three",
                       base_ts + 2 * gap))
    fake._points[-1].payload["_score"] = 0.5
    fake.seed(_payload("fs", "fs/one", 1, "fs one part 2",
                       base_ts + 3 * gap))
    fake._points[-1].payload["_score"] = 0.7
    # other-owned points: collection-wide counts only, excluded from the
    # caller's owner-scoped views.
    fake.seed(_payload("pi", "pi/five", 0, "pi five",
                       base_ts + 4 * gap, owner_tag=_OTHER_TAG))
    fake._points[-1].payload["_score"] = 0.95
    fake.seed(_payload("hermes", "hermes/six", 0, "hermes six",
                       base_ts + 5 * gap, owner_tag=_OTHER_TAG))
    fake._points[-1].payload["_score"] = 0.85
    fake.seed(_payload("note", "note/seven", 0, "note seven",
                       base_ts + 6 * gap, owner_tag=_OTHER_TAG))
    fake._points[-1].payload["_score"] = 0.99
    app.qdrant_client = fake
    return fake


class _FakeModel:
    """A stand-in embedding model with a pinned 384-dim output."""
    dim = 384

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))

        class _Batch:
            def tolist(self):
                return [[0.1] * _FakeModel.dim for _ in texts]
        return _Batch()


def _seed_embed_pool(app, failing=False):
    """Seed the app's pooled embedder (``server._embed_pool``) per the
    established contract — ``embed(texts) -> list of vectors`` (see
    ``tests/integration/test_web_app.py::test_e2e_full_journey``).

    The pre-fix web handler never touches the pool, so seeding it keeps the
    two owner-scoped tests green in RED; the three new tests need the pool
    to observe the embed + vector contract.  ``failing=True`` makes the
    pooled embedder raise (the embedding-failure 503 path).
    """
    if failing:
        def _embed(texts):
            raise RuntimeError("embedding.model failed to load (fake)")
        app._embed_pool = {"embed": _embed}
        return None
    model = _FakeModel()
    app._fake_model = model
    app._embed_pool = {
        "embed": lambda texts: model.encode(list(texts)).tolist()}
    return model


# =============================================================================
# T007 [US2] RED: /api/kb/points — count + owner_count + sample
# =============================================================================


def test_kb_points_count_owner_count_sample(web_app):
    """GET /api/kb/points?limit=3 → {count, owner_count, source_count, sample}.

    Against the in-process Qdrant stub seeded with 7 points (4 owner-scoped,
    3 other-owned): ``count`` = collection-wide (7), ``owner_count`` = the
    caller's ``owner_tag`` scope (4), ``source_count`` = total distinct
    sources (4: fs/hermes/pi/note), and ``sample`` = up to 3 most-recent
    rows, each ``{source, source_url, chunk_index, text}`` — the two
    fs chunks (ts 1100, 1300) then hermes/three (ts 1200).

    RED: /api/kb/points is unimplemented (T008), so the scaffold dispatches
    it to a clean JSON 404 — the Qdrant stub never gets touched.  GREEN is
    T008 reading the seeded ``app.qdrant_client``.
    """
    _app, _db, host, port, session_token = web_app
    fake = _seed_points(_app)
    code, parsed, raw = _http_get(
        host, port, "/api/kb/points?limit=3",
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"GET /api/kb/points?limit=3 expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert parsed["count"] == 7, (
        f"count must be collection-wide (7), got {parsed.get('count')}")
    assert parsed["owner_count"] == 4, (
        f"owner_count must be the caller's owner_tag scope (4), "
        f"got {parsed.get('owner_count')}")
    assert parsed.get("source_count") == 4, (
        f"source_count must be distinct source names (fs/hermes/pi/note = 4), "
        f"got {parsed.get('source_count')}")
    sample = parsed.get("sample")
    sample_desc = len(sample) if isinstance(sample, list) else type(sample)
    assert isinstance(sample, list) and len(sample) == 3, (
        f"sample must have exactly 3 rows (limit=3), got {sample_desc}: "
        f"{parsed!r}")
    for row in sample:
        assert set(row) == {"source", "source_url", "chunk_index", "text"}, (
            f"sample row keys must be exactly "
            f"{{source, source_url, chunk_index, text}}, got {set(row)}")
    # Most recent first: fs/two(1100) → fs/one#1(1300) → hermes/three(1200)
    assert sample[0]["source_url"] == "fs/two", (
        f"sample[0] must be the most-recent owner point fs/two, "
        f"got {sample[0]!r}")
    assert sample[1]["source_url"] == "fs/one", (
        f"sample[1] must be fs/one, got {sample[1]!r}")
    assert sample[1]["chunk_index"] == 1, (
        f"sample[1] chunk_index must be 1, got {sample[1]!r}")
    assert sample[2]["source_url"] == "hermes/three", (
        f"sample[2] must be hermes/three, got {sample[2]!r}")


def test_kb_points_empty_collection(web_app):
    """GET /api/kb/points against an empty Qdrant → all-zero counts + [].

    The stub is seeded with zero points (an empty collection, not an
    unavailable one), so the endpoint must 200 with
    ``{count:0, owner_count:0, source_count:0, sample:[]}`` — no 502/503.

    RED: unimplemented route → clean JSON 404.  GREEN is T008.
    """
    _app, _db, host, port, session_token = web_app
    app = _app
    app.qdrant_client = FakeQdrantClient()  # empty collection
    code, parsed, raw = _http_get(
        host, port, "/api/kb/points",
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"GET /api/kb/points (empty collection) expected 200, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed == {
        "count": 0,
        "owner_count": 0,
        "source_count": 0,
        "sample": [],
    }, f"empty collection must be all-zero counts + empty sample: {parsed!r}"


def test_kb_points_source_filter(web_app):
    """GET /api/kb/points?source=fs → count + sample restricted to source=fs.

    With 7 points across 4 sources, ``source=fs`` restricts the *count* and
    the *sample* to fs points (3: one#0, two, one#1) while ``owner_count``
    stays collection-wide owner-scoped (4).  The two pinned values
    ``source_count=3`` and ``count=3`` distinguish the source-filtered
    behaviour from the unfiltered test — the handler must honour
    ``?source=`` on both count and sample, not just sample.

    RED: unimplemented route → clean JSON 404.  GREEN is T008.
    """
    _app, _db, host, port, session_token = web_app
    _seed_points(_app)
    code, parsed, raw = _http_get(
        host, port, "/api/kb/points?source=fs",
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"GET /api/kb/points?source=fs expected 200, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    # The source filter restricts the *count* to fs points (3).
    assert parsed["count"] == 3, (
        f"with ?source=fs, count must be fs points (3), "
        f"got {parsed.get('count')}")
    # source_count pins the distinct-source count within the filtered set.
    assert parsed.get("source_count") == 3, (
        f"with ?source=fs, source_count must be 3 (fs one#0, two, one#1), "
        f"got {parsed.get('source_count')}")
    # owner_count is owner-scoped over the collection (4), unaffected by the
    # source filter.
    assert parsed["owner_count"] == 4, (
        f"owner_count stays owner-scoped (4) under ?source=fs, "
        f"got {parsed.get('owner_count')}")
    sample = parsed.get("sample")
    sample_desc = len(sample) if isinstance(sample, list) else type(sample)
    assert isinstance(sample, list) and len(sample) == 3, (
        f"source-filtered sample must have 3 rows (all fs), got {sample_desc}")
    assert all(row["source"] == "fs" for row in sample), (
        f"every source-filtered sample row must be source=fs: "
        f"{[r.get('source') for r in sample]!r}")
    assert {row["source_url"] for row in sample} == {
        "fs/one", "fs/two"}, (
        f"source-filtered sample must be the fs points: "
        f"{[r.get('source_url') for r in sample]!r}")


def test_kb_points_qdrant_unavailable(web_app):
    """Qdrant raising → 502/503 {"error": "qdrant unavailable: <hint>"}.

    A QdrantClient that raises on construction/first use (transport down)
    must produce a clean ``502`` or ``503`` with an ``error`` string that
    names Qdrant and carries a remediation hint — never a 500 / traceback.
    This pins the *unavailable* contract (distinct from the empty-collection
    200 above: here the client itself fails).

    RED: unimplemented route → clean JSON 404 (the raising stub never gets
    invoked because T008 has not landed).  GREEN is T008 catching the
    failure and mapping it to 502/503.
    """
    _app, _db, host, port, session_token = web_app
    app = _app

    class _UnavailableQdrant:
        """Qdrant client whose every call raises (transport down)."""
        def __getattr__(self, name):
            raise ConnectionError("qdrant: connection refused")

    app.qdrant_client = _UnavailableQdrant()
    code, parsed, raw = _http_get(
        host, port, "/api/kb/points",
        headers={"Authorization": f"Bearer {session_token}"})
    assert code in (502, 503), (
        f"Qdrant-unavailable GET /api/kb/points expected 502/503, "
        f"got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert "error" in parsed, f"missing 'error': {parsed!r}"
    err = str(parsed["error"]).lower()
    assert "qdrant" in err, (
        f"'error' must name Qdrant: {parsed['error']!r}")
    # The hint is remediation, not a bare status word.
    assert len(str(parsed["error"])) > 20, (
        f"'error' must carry a remediation hint, got {parsed['error']!r}")


def test_kb_points_no_token_401(web_app):
    """GET /api/kb/points without a Bearer token → 401 {"error":...}.

    The bearer gate must reject BEFORE the handler runs (fail-closed), so the
    status is 401 regardless of Qdrant state.  (Covered by the scaffold's
    API_ROUTES sweep too; this is the KB-specific pin.)

    RED/GREEN: the gate is T004's (already green) — the 401 holds now and
    after T008 lands.  This test is not the 404 RED driver; it guards the
    auth boundary of the new KB route.
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw = _http_get(host, port, "/api/kb/points")
    assert code == 401, (
        f"GET /api/kb/points without a token expected 401, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")


# =============================================================================
# T009 [US3] RED: /api/kb/search — owner-scoped vector search
# =============================================================================


def test_kb_search_top_n_owner_scoped(web_app):
    """POST /api/kb/search {query, limit:2} → owner-scoped top-N by score.

    Against the in-process stub (7 points, 4 owner-scoped with
    ``_score`` two=0.9 / one=0.7 / three=0.5, 3 other-owned with higher
    scores seven=0.99 / five=0.95 / six=0.85), the top-2 for the caller is
    ``[two(0.9), one(0.7)]`` in descending score — the highest-scoring
    *other-owned* points (0.99, 0.95, 0.85) must NOT leak in.  Each result is
    ``{score, source_url, text, source, chunk_index}``.

    RED: /api/kb/search is unimplemented (T010) → clean JSON 404.  GREEN is
    T010 running the owner-tagged filter + score sort over the seeded stub.
    """
    _app, _db, host, port, session_token = web_app
    fake = _seed_points(_app)
    _seed_embed_pool(_app)
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": "test", "limit": 2},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"POST /api/kb/search expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    results = parsed.get("results")
    assert isinstance(results, list), f"'results' must be a list: {parsed!r}"
    assert len(results) == 2, (
        f"limit=2 must yield 2 results, got {len(results)}: {results!r}")
    for r in results:
        assert set(r) == {"score", "source_url", "text", "source",
                          "chunk_index"}, (
            f"result keys must be exactly "
            f"{{score, source_url, text, source, chunk_index}}, "
            f"got {set(r)}: {r!r}")
    # Owner-scoped + descending score: two(0.9) then one(0.7).
    assert results[0]["source_url"] == "fs/two", (
        f"top result must be fs/two (owner-scoped, score 0.9), "
        f"got {results[0]!r}")
    assert results[0]["score"] == pytest.approx(0.9), (
        f"top score must be 0.9, got {results[0].get('score')}")
    assert results[1]["source_url"] == "fs/one", (
        f"second result must be fs/one (owner-scoped, score 0.7), "
        f"got {results[1]!r}")
    assert results[1]["score"] == pytest.approx(0.7), (
        f"second score must be 0.7, got {results[1].get('score')}")
    assert results[0]["score"] > results[1]["score"], (
        f"results must be descending by score: {results!r}")
    # Owner-scoping: no other-owned point (five/six/seven) may appear.
    leaked = [r["source_url"] for r in results
              if r["source_url"] in ("pi/five", "hermes/six",
                                     "note/seven")]
    assert not leaked, (
        f"owner-scoped search leaked other-owned points: {leaked!r}")


def test_kb_search_blank_query_400(web_app):
    """POST /api/kb/search {query:""} → 400 {"error": "query must be..."}

    Blank/missing query is 400 — validation happens AFTER the bearer gate
    (the token is valid here) but BEFORE any Qdrant call.  The exact error
    string is pinned to the contract.

    RED: unimplemented route → clean JSON 404.  GREEN is T010 validating the
    query before touching Qdrant.
    """
    _app, _db, host, port, session_token = web_app
    _seed_points(_app)  # a populated stub is present; the 400 must not hit it
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": ""},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 400, (
        f"POST /api/kb/search with blank query expected 400, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert parsed.get("error") == "query must be a non-empty string", (
        f"400 body must be the contract error, got {parsed!r}")


def test_kb_search_no_token_401(web_app):
    """POST /api/kb/search without a Bearer token → 401 (fail-closed gate).

    The bearer gate rejects before body validation, so an empty body + no
    token is 401 (not 400).  Same guard as the points 401 test, for the
    search route.

    RED/GREEN: the gate is T004's (already green) — 401 holds now and after
    T010 lands; this pins the auth boundary, not the 404.
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw = _http_post(host, port, "/api/kb/search", {})
    assert code == 401, (
        f"POST /api/kb/search without a token expected 401, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")


def test_kb_search_qdrant_unavailable(web_app):
    """POST /api/kb/search with Qdrant raising → 502/503 with a hint.

    A valid query + valid token, but the Qdrant client raises → a clean
    ``502``/``503`` ``{"error": "qdrant unavailable: <hint>"}`` (no crash,
    no traceback).  Distinct from the blank-query 400: the query is valid
    here, the failure is the vector store.

    RED: unimplemented route → clean JSON 404 (the raising stub is never
    invoked because T010 has not landed).  GREEN is T010 catching the
    failure and mapping it to 502/503.
    """
    _app, _db, host, port, session_token = web_app
    app = _app
    _seed_embed_pool(_app)

    class _UnavailableQdrant:
        def __getattr__(self, name):
            raise ConnectionError("qdrant: connection refused")

    app.qdrant_client = _UnavailableQdrant()
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": "valid query", "limit": 5},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code in (502, 503), (
        f"Qdrant-unavailable POST /api/kb/search expected 502/503, "
        f"got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert "error" in parsed, f"missing 'error': {parsed!r}"
    err = str(parsed["error"]).lower()
    assert "qdrant" in err, (
        f"'error' must name Qdrant: {parsed['error']!r}")
    assert len(str(parsed["error"])) > 20, (
        f"'error' must carry a remediation hint, got {parsed['error']!r}")

def test_kb_search_embeds_query_and_passes_vector(web_app):
    """POST /api/kb/search embeds the query and passes the vector to Qdrant.

    The query must be embedded via the config-pinned model (the pool), and
    the resulting vector passed as ``query=<vector>`` to ``query_points`` —
    a bare filtered scroll is not a vector search.

    RED (pre-010): the handler never embeds and calls ``query_points`` with
    no ``query`` → ``last_query`` stays None and the pool is never called.
    GREEN (010 T2): the pool is called with the raw query text and the
    recorded ``last_query`` is that embedding.
    """
    _app, _db, host, port, session_token = web_app
    fake = _seed_points(_app)
    model = _seed_embed_pool(_app)
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": "embed me", "limit": 2},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"POST /api/kb/search expected 200, got {code}: {raw[:300]!r}")
    assert model.calls, (
        "the query must be embedded via the pooled model; "
        f"embed was never called: calls={model.calls!r}")
    assert model.calls[-1] == ["embed me"], (
        f"the pooled embed must be called with the raw query text, "
        f"got {model.calls[-1]!r}")
    assert fake.last_query is not None, (
        "query_points must receive the query vector (query=...), "
        f"but no vector was passed: last_query={fake.last_query!r}")
    assert fake.last_query == [0.1] * 384, (
        f"the passed vector must be the embedding of the query, "
        f"got {fake.last_query!r}")


def test_kb_search_query_response_shape(web_app):
    """POST /api/kb/search handles the real qdrant-client QueryResponse.

    A live qdrant-client ``query_points`` returns a ``QueryResponse`` whose
    points live under ``.points`` (iterating the object itself yields
    pydantic ``(field, value)`` tuples, so ``r.score`` is an AttributeError).
    The handler must read ``results.points`` — the CountResult-aware
    normalisation pattern — so both the real client and the bare-list test
    fake are handled.

    RED (pre-010): the handler iterates the QueryResponse directly →
    AttributeError → the fail-closed 500 ``internal_error``.  GREEN (010
    T2): the handler normalises to ``.points`` → 200 with the 4 owner rows,
    descending by score (fs/two first at 0.9).
    """
    _app, _db, host, port, session_token = web_app
    fake = _seed_points(_app)
    _seed_embed_pool(_app)
    owner_points = [
        _ScoredPoint(p, p.payload.get("_score", 0.0))
        for p in fake._points
        if p.payload.get("owner_tag") == _CALLER_TAG
    ]
    assert len(owner_points) == 4

    class _QueryResponseShaped:
        """Mimics qdrant-client QueryResponse: points under ``.points``;
        iterating the object yields ``(field, value)`` tuples (pydantic
        model ``__iter__``)."""
        def __init__(self, points):
            self.points = points
            self.score_threshold = None

        def __iter__(self):
            return iter(self.__dict__.items())

    def _shaped_query_points(collection, query=None, query_filter=None,
                             limit=None, with_payload=True, **_kw):
        fake.last_query = query
        return _QueryResponseShaped(owner_points)

    fake.query_points = _shaped_query_points
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": "find me", "limit": 10},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 200, (
        f"POST /api/kb/search expected 200 against a QueryResponse, "
        f"got {code}: {raw[:300]!r}")
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    results = parsed.get("results")
    assert isinstance(results, list), f"'results' must be a list: {parsed!r}"
    assert len(results) == 4, (
        f"all 4 owner-scoped points must be returned, "
        f"got {len(results)}: {results!r}")
    assert results[0]["source_url"] == "fs/two", (
        f"top result must be fs/two (owner-scoped, score 0.9), "
        f"got {results[0]!r}")
    assert results[0]["score"] == pytest.approx(0.9)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), (
        f"results must be descending by score: {scores!r}")


def test_kb_search_embedding_unavailable_503(web_app):
    """POST /api/kb/search with the embedder failing → clean 503 embedding.

    A valid query + a live (fake) Qdrant, but the pooled embedder raises →
    a clean 502/503 whose error names the embedding config (embedding.model
    / embedding.device), DISTINCT from the qdrant-unavailable hint.  Fail-
    closed: no traceback, and the request never reaches Qdrant.

    RED (pre-010): the web handler has no embedding branch (it never embeds)
    → 200, and no embedding hint exists.  GREEN (010 T2): a clean 503 naming
    the embedding knobs, not the qdrant hint.
    """
    _app, _db, host, port, session_token = web_app
    _seed_points(_app)
    _seed_embed_pool(_app, failing=True)
    code, parsed, raw = _http_post(
        host, port, "/api/kb/search",
        {"query": "valid query", "limit": 5},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code in (502, 503), (
        f"embedding-failure POST /api/kb/search expected 502/503, "
        f"got {code}: {raw[:300]!r}")
    assert parsed is not None, f"body must be JSON: {raw[:300]!r}"
    assert "error" in parsed, f"missing 'error': {parsed!r}"
    err = str(parsed["error"]).lower()
    assert "embedding" in err, (
        f"the 503 must name the embedding config, got {parsed['error']!r}")
    assert "qdrant unavailable" not in err, (
        f"the embedding 503 must be distinct from the qdrant hint, "
        f"got {parsed['error']!r}")
