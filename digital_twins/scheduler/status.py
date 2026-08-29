"""Status endpoint: status_payload + ThreadingHTTPServer handler (002).

GET /status -> 200 JSON (the status_payload shape); 404 for any other path.
Content-Type: application/json. No auth in 002 (A3).

SC-004: the /status response must complete in < 500 ms wall-clock. The
payload builder is a few sqlite queries (list_schedules + one audit_runs
row) — no I/O beyond the local state db — so a single /status round-trip
comfortably fits under the 500 ms budget.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from digital_twins.scheduler.schedules import list_schedules

# Module-level server start, used when a caller does not pass an explicit
# ``start_time``. Set by :class:`StatusServer` at construction; a fresh
# ``status_payload(db, config)`` call (e.g. in a unit test that never built a
# server) falls back to the import time so ``uptime_s`` is always defined.
_SERVER_START = time.time()

# The audit_runs columns read for ``last_run`` (most recent row by started_at).
_AUDIT_COLUMNS = (
    "run_id", "started_at", "completed_at", "status",
    "trigger", "scheduled_by", "per_source_counts",
)


def status_payload(db, config, *, start_time: float | None = None) -> dict:
    """Build the /status payload (contracts/scheduler.md).

    Args:
        db: open 001 state connection (schedules + audit_runs, v2).
        config: resolved 001 config dict (unused by the builder in v1;
            accepted for forward-compatibility with later knobs).
        start_time: epoch seconds the server started; ``uptime_s`` is
            ``time.time() - start_time``. Defaults to the module-level
            server start (:data:`_SERVER_START`), which :class:`StatusServer`
            sets at construction.

    Returns:
        ``{"uptime_s": float, "schedules": [...], "last_run": {...} | None,
        "queue_depth": int}`` — see the module docstring for the shape.
    """
    start = _SERVER_START if start_time is None else float(start_time)
    uptime = time.time() - start

    schedules = [
        {
            "id": s["id"],
            "owner": s["owner"],
            "source": s["source"],
            "preset": s["preset"],
            "fire_time": s["fire_time"],
            "next_fire_at": s["next_fire_at"],
            "enabled": s["enabled"],
        }
        for s in list_schedules(db)
    ]

    row = db.execute(
        "SELECT " + ", ".join(_AUDIT_COLUMNS) +
        " FROM audit_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        last_run = None
    else:
        last_run = dict(zip(_AUDIT_COLUMNS, row))
        # per_source_counts is a JSON TEXT column; decode it to a dict (or
        # None when the run never finished / stored NULL). The keys of this
        # dict are the sources the run touched.
        raw = last_run.get("per_source_counts")
        last_run["per_source_counts"] = (
            json.loads(raw) if raw else None
        )
        # Key the run by its source (ruling R-09: "keyed by its source
        # field"). The audit_runs table has no dedicated source column in
        # v1; the run's source is the source of the schedule that triggered
        # it (trigger='schedule', scheduled_by=owner). Resolve it via the
        # owner's enabled schedules; manual runs have no single source.
        last_run["source"] = _run_source(db, last_run["run_id"])

    # queue_depth: 0 in v1 (single-fire-per-tick-per-schedule: every due
    # schedule is either fired or skipped within the same tick, so nothing
    # queues between ticks).
    return {
        "uptime_s": uptime,
        "schedules": schedules,
        "last_run": last_run,
        "queue_depth": 0,
    }


def _run_source(db, run_id: str) -> str | None:
    """Best-effort source for a run.

    The audit_runs table carries no source column in v1; the run's source
    is the source of the schedule that triggered it (``scheduled_by`` names
    the owner, not the source). When the run was triggered by a schedule
    (trigger='schedule'), resolve the owner's enabled schedules and return
    the first matching source; otherwise fall back to None (manual runs have
    no single source).
    """
    row = db.execute(
        "SELECT trigger FROM audit_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None or row[0] != "schedule":
        return None
    owner = db.execute(
        "SELECT scheduled_by FROM audit_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if owner is None:
        return None
    sched = db.execute(
        "SELECT source FROM schedules WHERE owner = ? AND enabled = 1 "
        "ORDER BY id ASC LIMIT 1",
        (owner[0],),
    ).fetchone()
    return sched[0] if sched else None


class _StatusHandler(BaseHTTPRequestHandler):
    """Serve GET /status as JSON; 404 for any other path.

    ``db`` and ``config`` are read off the server instance (set by
    :class:`StatusServer.__init__`) so the handler has the state it needs
    without per-request wiring. ``log_message`` is suppressed to keep
    stderr quiet during tests (and to avoid per-request I/O in the hot
    path, helping SC-004).
    """

    server_version = "digital-twins-status/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]  # ignore query string
        if path != "/status":
            self._send_json(404, {"error": "not found"})
            return
        with self.server._db_lock:
            payload = status_payload(
                self.server.db, self.server.config,
                start_time=self.server.start_time,
            )
        self._send_json(200, payload)

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 (http.server signature)
        # Suppress default stderr logging: no per-request noise during tests,
        # no I/O in the request hot path (SC-004).
        pass


class StatusServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that serves GET /status with status_payload.

    ``db`` and ``config`` are stored on the instance so the handler can read
    them per request. ``start_time`` is recorded at construction and drives
    ``uptime_s``.

    The db connection is opened with ``check_same_thread=False`` and all
    requests are serialized by a lock: ThreadingHTTPServer dispatches each
    request to a new thread, so the single SQLite connection (created in the
    caller's thread) would otherwise trip sqlite3's same-thread guard. The
    lock keeps concurrent /status requests from interleaving on the shared
    connection.

    Usage (T008):
        server = StatusServer(("127.0.0.1", port), db, config)
        server.start()
        ...
        server.stop()   # clean stop on serve shutdown (T007's stop path)
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db, config):
        self.config = config
        self.start_time = time.time()
        # The lock serializes concurrent /status requests on the shared
        # SQLite connection (each request runs in its own thread).
        self._db_lock = threading.Lock()
        # The daemon thread that runs serve_forever. Set by start(), read by
        # stop(). None until start() is called.
        self._thread = None
        # Keep the module-level default in sync so a stray status_payload
        # call without an explicit start_time still reports this server's
        # start (and so a second server doesn't clobber the first one's
        # value while it is still serving).
        global _SERVER_START
        _SERVER_START = self.start_time
        # Open our own read-only connection to the caller's db file with
        # check_same_thread=False so the handler thread (a different thread
        # from the caller's) can query it without tripping sqlite3's
        # same-thread guard. The caller's connection is used for the
        # initial read (in its own thread); subsequent reads in handler
        # threads use this connection.
        self.db = _open_same_db(db, check_same_thread=False)
        super().__init__(addr, _StatusHandler)

    def start(self) -> None:
        """Launch serve_forever on a daemon thread and return immediately.

        Called by ``run_serve`` (loop.py) before the first tick. The
        daemon thread keeps the event loop (serve_forever) running without
        blocking the caller; the caller can proceed to the tick loop and
        will later call :meth:`stop` to shut down.
        """
        if self._thread is not None and self._thread.is_alive():
            # Already serving — don't double-start.
            return
        self._thread = threading.Thread(
            target=self.serve_forever, daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the serve loop and join the thread.

        Called by ``run_serve`` (loop.py) on the shutdown path. shutdown()
        is a ThreadingHTTPServer method that stops serve_forever; we then
        join the thread so the daemon has fully exited before the caller
        moves on. Idempotent: safe to call even if start() was never called
        or the thread already exited.
        """
        # shutdown() blocks until serve_forever returns; only call it if the
        # thread is actually running (a fresh/never-started server has no
        # serve_forever loop to stop, and calling shutdown() on a server that
        # never served would just return immediately — but we guard anyway
        # to keep the contract obvious).
        try:
            self.shutdown()
        except Exception:
            # shutdown() can raise if the server was never started or already
            # stopped; treat that as a clean no-op.
            pass
        if self._thread is not None:
            self._thread.join()
            self._thread = None


def _open_same_db(db, check_same_thread: bool = False):
    """Open a new connection to the same SQLite file as ``db``.

    ``check_same_thread`` controls whether the new connection can be used
    from threads other than the one that opened it (default: False, so
    handler threads can query it). In-memory databases (no file) cannot be
    reopened from another thread, so the original connection is returned as
   -is — the caller must have opened it with ``check_same_thread=False``.
    """
    # The db file path: derive it from the caller's connection via the
    # main database entry in PRAGMA database_list.
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        # row = (seq, name, file)
        path = row[2] if row and row[2] else None
    except Exception:
        path = None

    if path is None or path == ":memory:":
        # In-memory db: can't reopen from another thread. Return the
        # original connection and rely on it having been opened with
        # check_same_thread=False (the test helper does this).
        return db

    return sqlite3.connect(str(path), check_same_thread=check_same_thread)
