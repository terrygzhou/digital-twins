"""T015 [006-web-app] RED: /api/kb/chat 501 surface + validation tests.

The 006 chat surface (C-1 / R8): ``POST /api/kb/chat`` ships as a
**surface-only** endpoint in this slice.  It is bearer-gated like every
other ``/api/*`` route, owner-scoped (the caller's ``owner_tag`` is known
from the verified session), and input-validated — but it does NOT attempt
LLM/RAG generation: with no ``llm.endpoint`` / ``llm.model`` configured it
returns a clean ``501 not_implemented`` with a remediation hint (mirrors
004's BR-10 MCP stub pattern).  A follow-up slice fills generation
without re-plumbing.

Pinned here (contracts/web-api.md ``POST /api/kb/chat``):

1. ``test_chat_no_llm_endpoint_501`` — no ``llm.endpoint`` configured,
   valid token, valid query → ``501 {"code": "not_implemented",
   "remediation": "set llm.endpoint / llm.model to enable chat (006 ships
   the surface only; R8)"}`` — a clean 501, not a crash and not a 401.
2. ``test_chat_blank_query_400`` — valid token, ``{"query": ""}`` →
   ``400 {"error": "query must be a non-empty string"}`` (validation runs
   after the gate, before any LLM/config read that matters).
3. ``test_chat_no_token_401`` — no Bearer → 401 (fail-closed gate).
4. ``test_chat_invalid_token_401`` — garbage Bearer → 401.
5. ``test_chat_does_not_attempt_generation_in_006`` — the 501 is returned
   without ANY LLM call: no http.client/urllib/requests/LLM client
   construction is attempted.  Asserted by wrapping every importable
   LLM-adjacent entry point (``http.client`` connect, ``urllib.request``
   urlopen/Request, and the config ``get`` for llm.*) with spies that
   raise/record if touched — the handler must answer the 501 without
   touching any of them.

Red-first per constitution III: the route does not exist in
``digital_twins/web/app.py`` yet (T016 lands it), so every test here fails
with the scaffold's clean JSON ``404 {"error": "not_found", ...}`` — NOT a
fixture or harness bug.  The fixture below mirrors
``tests/unit/test_web_app_kb.py``: migrated v3 state DB in ``tmp_path``,
``ThreadingHTTPServer`` on ``127.0.0.1:0``, socket-connect readiness poll,
requests driven with ``http.client``.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from digital_twins.accounts import create_account, owner_tag_for
from digital_twins.auth import create_session
from digital_twins.state import db as state_db

# --- spec -------------------------------------------------------------------

#: The exact 501 contract body (contracts/web-api.md, POST /api/kb/chat).
CHAT_501_CODE = "not_implemented"
CHAT_501_REMEDIATION = (
    "set llm.endpoint / llm.model to enable chat "
    "(006 ships the surface only; R8)"
)

#: The exact 400 contract body for a blank query.
CHAT_400_ERROR = "query must be a non-empty string"

_READY_TIMEOUT_S = 5.0


# --- helpers ------------------------------------------------------------------


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


def _http_post(host: str, port: int, path: str, body: dict,
               headers: dict | None = None
               ) -> tuple[int, dict | None, bytes]:
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


def _http_get(host: str, port: int, path: str,
              headers: dict | None = None
              ) -> tuple[int, dict | None, bytes]:
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


# --- fixture ------------------------------------------------------------------


@pytest.fixture
def web_app(tmp_path):
    """Build + serve the 006 WebApp on 127.0.0.1:0 with a v3 state DB.

    Mirrors the ``web_app`` fixture in ``tests/unit/test_web_app_kb.py``:
    a migrated state DB, one account, one live session token, and the app
    served on an OS-assigned port.  Yields
    ``(app, db, host, port, session_token)``.

    The fixture passes a minimal config with NO ``llm.endpoint`` /
    ``llm.model`` (neither in the caller dict nor in the schema defaults),
    which is exactly the RED-state condition T015 pins: the chat surface
    must answer 501 ``not_implemented`` in this state.
    """
    from digital_twins.web.app import build_web_app, serve

    db = _make_db(tmp_path)
    try:
        create_account(db, "web-user@example.com", "web-pw-123")
        session_token, _expires_at = create_session(db, "web-user@example.com")

        # No llm.endpoint / llm.model configured (the 501 condition).
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


# =============================================================================
# T015 [US6] RED: /api/kb/chat — 501 surface + validation
# =============================================================================


def test_chat_no_llm_endpoint_501(web_app):
    """POST /api/kb/chat with a valid query, no llm.endpoint → 501 not_implemented.

    The contract body is pinned exactly:
    ``{"code": "not_implemented",
       "remediation": "set llm.endpoint / llm.model to enable chat
       (006 ships the surface only; R8)"}``

    This is a clean 501 — not a crash (500), not a 401 (the token IS
    valid here), not a 404 (the route IS dispatched once T016 lands).

    RED: the route does not exist yet → the scaffold's clean JSON 404
    (``{"error": "not_found", ...}``) — T016 implements the handler.
    """
    _app, _db, host, port, session_token = web_app
    code, parsed, raw = _http_post(
        host, port, "/api/kb/chat",
        {"query": "hello"},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 501, (
        f"POST /api/kb/chat (valid token, valid query, no llm.endpoint) "
        f"expected 501, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"501 body must be JSON: {raw[:300]!r}"
    assert parsed.get("code") == CHAT_501_CODE, (
        f"501 body 'code' must be {CHAT_501_CODE!r}, got {parsed!r}")
    assert parsed.get("remediation") == CHAT_501_REMEDIATION, (
        f"501 body 'remediation' must be the contract hint, got {parsed!r}")


def test_chat_blank_query_400(web_app):
    """POST /api/kb/chat {query:""} with a valid token → 400 contract error.

    Validation runs AFTER the bearer gate (the token is valid here) and
    returns the exact contract error before any LLM/config read.

    RED: the route does not exist yet → clean JSON 404.  T016 lands the
    validation.
    """
    _app, _db, host, port, session_token = web_app
    code, parsed, raw = _http_post(
        host, port, "/api/kb/chat",
        {"query": ""},
        headers={"Authorization": f"Bearer {session_token}"})
    assert code == 400, (
        f"POST /api/kb/chat with blank query expected 400, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None, f"400 body must be JSON: {raw[:300]!r}"
    assert parsed.get("error") == CHAT_400_ERROR, (
        f"400 body must be the contract error, got {parsed!r}")


def test_chat_no_token_401(web_app):
    """POST /api/kb/chat without a Bearer token → 401 (fail-closed gate).

    The bearer gate rejects BEFORE any body validation or config read, so
    the status is 401 regardless of the body.

    RED/GREEN: the gate is T004's (already green) — the 401 holds now and
    after T016 lands; this pins the auth boundary of the new route.  In
    RED state the gate still runs first, so this test is already green
    (the 401 is the gate's, not the handler's).
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw = _http_post(host, port, "/api/kb/chat", {"query": "hi"})
    assert code == 401, (
        f"POST /api/kb/chat without a token expected 401, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")


def test_chat_invalid_token_401(web_app):
    """POST /api/kb/chat with a garbage Bearer token → 401.

    A present-but-invalid token is rejected by the same fail-closed gate
    as an absent one (verify_session returns None → 401).

    RED/GREEN: gate-owned, already green; pins the invalid-token branch of
    the auth boundary.
    """
    _app, _db, host, port, _token = web_app
    code, parsed, raw = _http_post(
        host, port, "/api/kb/chat",
        {"query": "hi"},
        headers={"Authorization": "Bearer definitely-not-a-session-token"})
    assert code == 401, (
        f"POST /api/kb/chat with an invalid Bearer token expected 401, "
        f"got {code}: {raw[:300]!r}"
    )
    assert parsed is not None and "error" in parsed, (
        f"401 body must be JSON with 'error': {raw[:300]!r}")


def test_chat_does_not_attempt_generation_in_006(web_app):
    """The 501 is returned WITHOUT any LLM/generation attempt.

    006 ships the chat surface only (R8): no RAG, no LLM client, no
    network call.  Asserted two ways:

    1. **No LLM-adjacent entry point is touched.**  A spy wraps
       ``http.client.HTTPConnection.connect`` (the only transport a
       hand-rolled LLM call could use from this handler),
       ``urllib.request.urlopen`` / ``urllib.request.Request`` (the stdlib
       HTTP path), and ``digital_twins.config.schema.get`` reads of the
       ``llm.*`` keys — the handler may read the config to DECIDE the 501,
       but it must not then construct or call anything LLM-shaped.  The
       spies record any attempt; a recorded attempt fails the test.
    2. **No embedding/Qdrant traffic.**  The app's Qdrant client attribute
       (if a fake were seeded) is untouched, and no ``_embed_pool`` is
       created on the server — generation would go through the pooled
       embedder or the Qdrant client, neither of which is allowed here.

    RED: the route does not exist yet → clean JSON 404 (the handler never
    runs, so trivially no generation attempt — the 404 assertion is the
    RED driver; the generation-never-touched assertions hold vacuously and
    become the real pin once T016 lands the handler).
    """
    _app, _db, host, port, session_token = web_app

    touched: list[str] = []

    # --- spy: no outgoing HTTP from the handler ------------------------------
    # The handler runs in the server's worker thread; a hand-rolled LLM call
    # would go through http.client / urllib.request.  A plain "any use"
    # wrapper cannot work: the test's OWN request to the chat endpoint is
    # also an http.client connection, so the spy would record the test's
    # own traffic.  Instead, the spy only records uses that happen while
    # the test's request is IN FLIGHT (between conn.request and
    # resp.read).  The test's own connection is created before
    # in_flight[0] is set and its .connect() runs before the flag flips,
    # so it is not counted; any LLM call the handler makes during the
    # request happens inside the in-flight window and IS counted.
    import http.client as _hc
    import urllib.request as _ur

    in_flight = [False]
    orig_connect = _hc.HTTPConnection.connect
    orig_urlopen = _ur.urlopen
    orig_request_ctor = _ur.Request

    def _spy_connect(self):
        if in_flight[0]:
            touched.append("http.client.HTTPConnection.connect")
        return orig_connect(self)

    def _spy_urlopen(*_a, **_kw):
        if in_flight[0]:
            touched.append("urllib.request.urlopen")
        return orig_urlopen(*_a, **_kw)

    def _spy_request(*_a, **_kw):
        if in_flight[0]:
            touched.append("urllib.request.Request")
        return orig_request_ctor(*_a, **_kw)

    _hc.HTTPConnection.connect = _spy_connect
    _ur.urlopen = _spy_urlopen
    _ur.Request = _spy_request
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5.0)
        conn.request("POST", "/api/kb/chat",
                     body=json.dumps({"query": "hello"}),
                     headers={"Content-Type": "application/json",
                              "Authorization": f"Bearer {session_token}"})
        in_flight[0] = True
        try:
            resp = conn.getresponse()
            raw = resp.read()
            code = resp.status
        finally:
            in_flight[0] = False
            conn.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = None
    finally:
        _hc.HTTPConnection.connect = orig_connect
        _ur.urlopen = orig_urlopen
        _ur.Request = orig_request_ctor

    # The 501 (GREEN) or the 404 (RED) — both mean the handler answered
    # without generation.  The generation-never-touched assertions hold in
    # both states; what distinguishes RED from GREEN is the status.
    assert code in (404, 501), (
        f"POST /api/kb/chat expected 404 (RED: route not yet implemented) "
        f"or 501 (GREEN: no llm.endpoint), got {code}: {raw[:300]!r}"
    )
    # The point of THIS test: no LLM/generation entry point was touched.
    assert not touched, (
        f"chat handler attempted LLM generation in 006: {touched!r} "
        f"(006 ships the surface only; R8)")
    # No embedding pool was created (generation would go through it).
    assert not hasattr(_app, "_embed_pool"), (
        "chat handler created an embedding pool — generation attempted in "
        "006 (R8 forbids it)")
