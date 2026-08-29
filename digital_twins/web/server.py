"""Web server: /signup + /signin (session tokens) + status passthrough.

003 multi-user minimal web UI (A2/R4).  A ``ThreadingHTTPServer`` that
serves three endpoints:

- ``POST /signup`` ``{email, password}`` → ``200 {"email","role",
  "created":true}``.  Role is resolved by the shared helper
  (first row in ``accounts`` → admin, else reader — C-4, shared with
  the CLI signup).  Duplicate email → ``409 {"error":"account
  already exists"}``.  The password is hashed with 001
  ``hash_password`` (R1).
- ``POST /signin`` ``{email, password}`` → ``200 {"session_token",
  "expires_at"}``.  Authenticates via 001 ``auth.authenticate``
  (password).  On success: creates a 32-byte session token, stores its
  pbkdf2 hash in ``sessions``, returns the **plaintext** token once.
  Sets ``accounts.last_active``.  On failure → ``401``.
- ``GET /status`` → reuses 002 ``status_payload`` + the C-5
  ``auth_checker`` (a valid session / personal / service token, else
  401).  Backward-compatible: when ``auth_checker`` is ``None`` (002's
  callers), the ``/status`` path is byte-for-byte the 002 behavior.

The session token is **short-lived** (default 8 h, ``expires_at``) and
**revocable** (``sessions.revoked``) — distinct from a personal token
(which has no TTL) and from the shared service token (static, BR-10).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from digital_twins.accounts import DuplicateEmailError, create_account
from digital_twins.auth import authenticate, create_session
from digital_twins.scheduler.status import status_payload

# The audit_runs columns read for ``last_run`` (most recent row by
# started_at).  Mirrors the 002 StatusServer's constant.
_AUDIT_COLUMNS = (
    "run_id", "started_at", "completed_at", "status",
    "trigger", "scheduled_by", "per_source_counts",
)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp, seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _WebHandler(BaseHTTPRequestHandler):
    """Serve POST /signup, POST /signin, GET /status as JSON.

    ``db`` and ``config`` are read off the server instance (set by
    :class:`WebServer.__init__`) so the handler has the state it needs
    without per-request wiring.  ``log_message`` is suppressed to keep
    stderr quiet during tests (and to avoid per-request I/O in the hot
    path).
    """

    server_version = "digital-twins-web/1.0"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]  # ignore query string
        if path == "/signup":
            self._handle_signup()
        elif path == "/signin":
            self._handle_signin()
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]  # ignore query string
        if path != "/status":
            self._send_json(404, {"error": "not found"})
            return
        # 003 C-5 / R8: gate on the auth_checker when one is installed.
        checker = getattr(self.server, "auth_checker", None)
        if checker is not None:
            headers = {k: v for k, v in self.headers.items()}
            try:
                result = checker(headers)
            except Exception:
                # A checker that raises is treated as a deny (401) — fail
                # closed: never serve a payload on an auth error.
                self._send_json(401, {"error": "unauthorized"})
                return
            if result is True:
                pass  # fall through to the 200 payload
            elif isinstance(result, str):
                self._send_json(403, {"error": result})
                return
            else:
                self._send_json(401, {"error": "unauthorized"})
                return
        with self.server._db_lock:
            payload = status_payload(
                self.server.db, self.server.config,
                start_time=self.server.start_time,
            )
        self._send_json(200, payload)

    # --- POST /signup --------------------------------------------------------

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
        role = None  # let create_account resolve: first row -> admin
        try:
            with self.server._db_lock:
                create_account(self.server.db, email, password, role=role)
        except DuplicateEmailError:
            self._send_json(409, {"error": "account already exists"})
            return
        # Re-read the role that was actually assigned (create_account
        # resolves it internally).
        with self.server._db_lock:
            row = self.server.db.execute(
                "SELECT role FROM accounts WHERE email=?", (email,)
            ).fetchone()
            assigned_role = row[0] if row else "reader"
        self._send_json(200, {
            "email": email,
            "role": assigned_role,
            "created": True,
        })

    # --- POST /signin --------------------------------------------------------

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
            session_token, expires_at = create_session(
                self.server.db, email)
            # Set accounts.last_active (the brief requires this on sign-in).
            self.server.db.execute(
                "UPDATE accounts SET last_active=? WHERE email=?",
                (_now_iso(), email),
            )
            self.server.db.commit()
        self._send_json(200, {
            "session_token": session_token,
            "expires_at": expires_at,
        })

    # --- helpers -------------------------------------------------------------

    def _read_json_body(self) -> dict | None:
        """Read and parse the request body as JSON.

        Returns the parsed dict, or ``None`` on any parse error.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 (http.server signature)
        # Suppress default stderr logging: no per-request noise during tests,
        # no I/O in the request hot path.
        pass


class WebServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that serves POST /signup, POST /signin, GET /status.

    ``db`` and ``config`` are stored on the instance so the handler can read
    them per request.  ``start_time`` is recorded at construction and drives
    ``uptime_s`` in the /status payload.

    The db connection is opened with ``check_same_thread=False`` and all
    requests are serialized by a lock: ThreadingHTTPServer dispatches each
    request to a new thread, so the single SQLite connection (created in the
    caller's thread) would otherwise trip sqlite3's same-thread guard.  The
    lock keeps concurrent requests from interleaving on the shared
    connection.

    003 C-5 / R8: ``auth_checker`` is an optional callable
    ``auth_checker(headers: dict[str, str]) -> bool | str``.  ``True`` allows
    the request (200); a ``str`` denies it with that string as the 403 body
    error; ``False`` denies it with a generic "unauthorized" (401).  When
    ``None`` (the default — 002's callers), no auth check is performed on
    ``/status`` and it behaves exactly as 002 (C-5 backward-compat).

    Usage:
        server = WebServer(("127.0.0.1", port), db, config)
        server.start()
        ...
        server.stop()   # clean stop on shutdown
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db, config, *, auth_checker=None):
        self.config = config
        self.start_time = time.time()
        self.auth_checker = auth_checker
        self._db_lock = threading.Lock()
        self._thread = None
        self.db = _open_same_db(db, check_same_thread=False)
        super().__init__(addr, _WebHandler)

    def start(self) -> None:
        """Launch serve_forever on a daemon thread and return immediately."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.serve_forever, daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the serve loop and join the thread."""
        try:
            self.shutdown()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join()
            self._thread = None


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

    return sqlite3.connect(str(path), check_same_thread=check_same_thread)
