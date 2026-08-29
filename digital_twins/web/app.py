"""Web app: static UI routes + the token-gated ``/api/*`` REST surface.

006 first-class web UI (BR-11.1.8), T004 scaffold.  A ``ThreadingHTTPServer``
+ ``BaseHTTPRequestHandler`` app (the 003 ``web/server.py`` model — no new
web framework) that routes:

- ``GET /`` → ``web/static/index.html`` (the asset lands in T020; until then
  the route still dispatches and returns a clean JSON 404 with
  ``Content-Type: text/html`` — never a traceback).
- ``GET /static/*`` → ``web/static/`` assets (path-traversal-safe: ``..``
  segments and absolute paths are rejected with 400; a missing asset is a
  clean JSON 404; the static route is unauthenticated).
- ``GET``/``POST /api/*`` → the REST handler dispatch.  Every route EXCEPT
  the public ``/api/auth/*`` credential endpoints first verifies the
  session token (FR-017, fail-closed): ``Authorization: Bearer <token>``
  header or ``?token=<token>`` query fallback, checked via
  ``digital_twins.auth.verify_session`` (the same helper 003's
  ``auth_checker`` uses).  Absent/expired/revoked → ``401
  {"error": "unauthorized"}``.  The handlers themselves land in
  T005–T016; until then, a gated-but-unimplemented ``/api/*`` route returns
  ``404 {"error": "not_found", "path": "<path>"}``.

The 003 credential endpoints are re-exposed under ``/api/auth/*`` by
reusing ``digital_twins.accounts`` / ``digital_twins.auth`` directly (R2:
one credential source of truth; ``server.py`` is not rewritten).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from digital_twins.accounts import DuplicateEmailError, create_account, get_role
from digital_twins.auth import (
    authenticate,
    create_session,
    revoke_session,
    verify_session,
)


# The static-asset root: ``digital_twins/web/static/`` (C-2/R4).  The actual
# assets (index.html / style.css) land in T020; until then the routes return
# a clean JSON 404, which keeps the scaffold's contract testable.
_STATIC_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_INDEX_PATH = os.path.join(_STATIC_ROOT, "index.html")

# 003 C-5 / T006: the public credential endpoints, exempt from the bearer
# gate (contracts/web-api.md — the only /api/* surface reachable without a
# session token).
_AUTH_EXEMPT = (
    "/api/auth/signup",
    "/api/auth/signin",
    "/api/auth/signout",
)

_MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _content_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _MIME_TYPES.get(ext, "application/octet-stream")


class _WebAppHandler(BaseHTTPRequestHandler):
    """Route the 006 web app: static UI + the bearer-gated /api/* REST surface.

    ``db``/``config``/``_db_lock`` are read off the server instance (set by
    :class:`WebApp.__init__`) so the handler has state without per-request
    wiring.  ``log_message`` is suppressed to keep stderr quiet during tests
    and to avoid per-request I/O in the hot path (mirrors 003 ``web/server.py``).
    """

    server_version = "digital-twins-web-app/1.0"
    sys_version = ""

    # --- dispatch -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        self._route("POST")

    def _route(self, method: str) -> None:
        raw_path, _, query = self.path.partition("?")
        try:
            if raw_path.startswith("/api/"):
                self._handle_api(method, raw_path, query)
            elif raw_path == "/":
                self._serve_index()
            elif raw_path.startswith("/static/"):
                self._serve_static(raw_path)
            else:
                self._send_bytes(404, "text/html",
                                 json.dumps(
                                     {"error": "not_found", "path": raw_path}
                                 ).encode("utf-8"))
        except Exception:
            # Fail-closed: any handler error is a clean 500 JSON error, never
            # a traceback in the response.
            self._send_bytes(
                500, "application/json",
                json.dumps({"error": "internal_error"}).encode("utf-8"))

    # --- GET / : the static UI index ------------------------------------------

    def _serve_index(self) -> None:
        data = self._read_asset(_INDEX_PATH)
        if data is None:
            # Asset missing (until T020): clean JSON 404, still text/html so
            # the route's content-type contract holds before the asset lands.
            self._send_bytes(404, "text/html",
                             json.dumps(
                                 {"error": "not_found", "path": "/"}
                             ).encode("utf-8"))
            return
        self._send_bytes(200, "text/html", data)

    # --- GET /static/* : static assets (path-traversal-safe) -------------------

    def _serve_static(self, raw_path: str) -> None:
        # Decode percent-escapes, then reject traversal and absolute paths.
        relative = urllib.parse.unquote(raw_path[len("/static/"):])
        if not relative or relative.startswith("/"):
            self._send_json(400, {"error": "invalid path"})
            return
        # Normalize (also collapses any encoded ".." that unquote exposed).
        cleaned = os.path.normpath(relative).lstrip("/")
        parts = cleaned.split(os.sep)
        if any(p in ("..", "") for p in parts) or ".." in cleaned:
            self._send_json(400, {"error": "invalid path"})
            return
        full = os.path.normpath(os.path.join(_STATIC_ROOT, cleaned))
        if not os.path.abspath(full).startswith(
                os.path.abspath(_STATIC_ROOT) + os.sep):
            self._send_json(400, {"error": "invalid path"})
            return
        if not os.path.isfile(full):
            self._send_json(404, {"error": "not_found", "path": raw_path})
            return
        with open(full, "rb") as fh:
            data = fh.read()
        self._send_bytes(200, _content_type_for(full), data)

    # --- /api/* : bearer gate first, then handler dispatch ----------------------

    def _handle_api(self, method: str, path: str, query: str) -> None:
        if path in _AUTH_EXEMPT:
            # Public credential endpoints: no session gate (FR-017 exempt).
            self._dispatch_auth(method, path, query)
            return
        # Bearer gate (FR-017): verify BEFORE any handler runs, so an
        # unauthenticated request is 401 even for unimplemented routes.
        token = self._extract_token(query)
        with self.server._db_lock:
            caller = verify_session(self.server.db, token) if token else None
        if caller is None:
            self._send_json(401, {"error": "unauthorized"})
            return
        self._dispatch_rest(method, path, query, caller)

    def _dispatch_auth(self, method: str, path: str, query: str) -> None:
        if path == "/api/auth/signup" and method == "POST":
            self._handle_signup()
            return
        if path == "/api/auth/signin" and method == "POST":
            self._handle_signin()
            return
        if path == "/api/auth/signout" and method == "POST":
            self._handle_signout()
            return
        self._send_json(404, {"error": "not_found", "path": path})

    def _dispatch_rest(self, method: str, path: str, query: str,
                       caller_email: str) -> None:
        # T004 scaffold: the REST handlers (/api/me, /api/kb/points, ...) land
        # in T005–T016.  Until then a gated-but-unimplemented route is a
        # clean JSON 404 — the gate has already run (no 401 past this point).
        del method, query, caller_email  # wired up by the later handler tasks
        self._send_json(404, {"error": "not_found", "path": path})

    # --- /api/auth/* handlers (003 credential semantics, R2) --------------------

    def _handle_signup(self) -> None:
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        email = body.get("email")
        password = body.get("password")
        if not email or not password:
            self._send_json(400, {"error": "email and password required"})
            return
        try:
            with self.server._db_lock:
                create_account(self.server.db, email, password, role=None)
                role = get_role(self.server.db, email) or "reader"
        except DuplicateEmailError:
            self._send_json(409, {"error": "account already exists"})
            return
        self._send_json(200, {"email": email, "role": role, "created": True})

    def _handle_signin(self) -> None:
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        email = body.get("email")
        password = body.get("password")
        if not email or not password:
            self._send_json(400, {"error": "email and password required"})
            return
        with self.server._db_lock:
            if not authenticate(self.server.db, email, password):
                self._send_json(401, {"error": "invalid credentials"})
                return
            session_token, expires_at = create_session(self.server.db, email)
        self._send_json(200, {
            "session_token": session_token,
            "expires_at": expires_at,
        })

    def _handle_signout(self) -> None:
        # FR-017: signout needs a valid token to revoke — fail-closed (401
        # with no/invalid token is T005's pin; the gate here is the same one).
        token = self._extract_token(self.path.partition("?")[2])
        with self.server._db_lock:
            caller = verify_session(self.server.db, token) if token else None
        if caller is None:
            self._send_json(401, {"error": "unauthorized"})
            return
        with self.server._db_lock:
            revoke_session(self.server.db, token)
        self._send_json(200, {"revoked": True})

    # --- helpers -------------------------------------------------------------

    def _extract_token(self, query: str) -> str | None:
        """Read the session token: Bearer header first, ?token= fallback."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
            if token:
                return token
        params = urllib.parse.parse_qs(query)
        values = params.get("token")
        if values and values[0]:
            return values[0]
        return None

    def _read_json_body(self) -> dict | None:
        """Read and parse the request body as JSON; None on any parse error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _read_asset(self, path: str) -> bytes | None:
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def _send_json(self, code: int, body: dict) -> None:
        self._send_bytes(code, "application/json",
                         json.dumps(body).encode("utf-8"))

    def _send_bytes(self, code: int, content_type: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 (http.server signature)
        # Suppress default stderr logging: no per-request noise during tests,
        # no I/O in the request hot path.
        pass


class WebApp(ThreadingHTTPServer):
    """ThreadingHTTPServer that serves the 006 web UI + /api/* REST surface.

    Mirrors 003's ``WebServer``: ``db`` and ``config`` live on the instance;
    the db connection is opened with ``check_same_thread=False`` (the 003
    ``_open_same_db`` helper) so request threads can use it, and all
    requests are serialized by ``_db_lock`` to keep concurrent requests from
    interleaving on the shared SQLite connection.

    ``server_address``/``shutdown``/``server_close`` are the
    ``ThreadingHTTPServer`` surface the tests and the CLI rely on; see
    :func:`build_web_app` and :func:`serve`.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db, config):
        self.config = config
        self.start_time = time.time()
        self._db_lock = threading.Lock()
        self._thread = None
        self.db = _open_same_db(db, check_same_thread=False)
        super().__init__(addr, _WebAppHandler)


def _open_same_db(db, check_same_thread: bool = False):
    """Open a new connection to the same SQLite file as ``db``.

    ``check_same_thread`` controls whether the new connection can be used
    from threads other than the one that opened it.  In-memory databases
    (no file) cannot be reopened from another thread, so the original
    connection is returned as-is.
    """
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        path = row[2] if row and row[2] else None
    except Exception:
        path = None

    if path is None or path == ":memory:":
        return db

    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def build_web_app(db, cfg, host: str = "127.0.0.1", port: int = 0) -> WebApp:
    """Build (but do not start) the WebApp on ``host:port``.

    ``port=0`` lets the OS assign a free port — the tests read it back off
    ``app.server_address``.
    """
    return WebApp((host, port), db, cfg)


def serve(app: WebApp) -> None:
    """Start serving on a daemon thread and return immediately.

    Mirrors 003 ``web/server.py``'s ``WebServer.start``: the serve loop runs
    on a daemon thread so callers keep control of the calling thread; the
    server itself exposes ``shutdown``/``server_close`` for a clean stop.
    """
    if app._thread is not None and app._thread.is_alive():
        return
    app._thread = threading.Thread(
        target=app.serve_forever, daemon=True,
    )
    app._thread.start()
