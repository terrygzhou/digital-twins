"""Serve loop: due-schedule detection, pipeline fire, pidfile guard (002).

std loop, tick ~3 s; in-process queue (R1, A4).

``serve_once_tick`` (T006) is one pass: due_schedules -> run 001 pipeline
per schedule (shared state, trigger='schedule', scheduled_by=schedule owner,
source=schedule.source) -> claim_and_advance. Per-source caps/timeouts are
read fresh from ``config`` on every tick (no caching): the caller (run_serve,
T007) re-loads config each tick, so cap changes are honored without restart
(SC-005).

Error handling (002 ruling R-07, "reported, never silent"):
    A source prerequisite failure (001 ``PrerequisiteError``) or any other
    unexpected pipeline exception for a due schedule writes a ``failed``
    audit row for that run and STILL advances the schedule via
    claim_and_advance, so it is not re-fired on the next tick. The schedule
    id lands in ``fired``: the fire WAS processed; the run failed but was
    audited.

``run_serve`` (T007) runs the serve loop: pidfile guard -> status server
-> tick loop until SIGTERM/SIGINT.
"""
from __future__ import annotations

import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from digital_twins.config.schema import get
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.scheduler.schedules import claim_and_advance, due_schedules
from digital_twins.state.models import finish_audit_run, start_audit_run
from digital_twins.user_config import merge_user_config

# Load config fresh on every tick (SC-005: cap/pause changes honored without
# restart). Named `load_config` here (not `load`) so a monkeypatch of
# `loop.load_config` cannot collide with anything imported into this module
# and tests can patch exactly this name.
from digital_twins.config.loader import load as load_config

# Tick cadence: ~3 s, a module constant (not a magic number inline). The
# default tick_seconds for run_serve; tests pass a smaller value.
TICK_SECONDS = 3.0

# Stop flag for the serve loop. A module-level flag (set by the signal
# handler, polled by the loop) rather than a ``threading.Event``: the signal
# handler runs in the MAIN thread, while the loop may run in a worker thread
# (unit tests). A ``threading.Event`` is only interruptible in the thread
# that called ``wait()``; a polled flag works in both. The loop checks it
# each tick and on a short wait interval between ticks, so a signal is
# honored within ~_STOP_POLL_S.
_stop_flag = False


def _reset_stop_flag() -> None:
    """Clear the module-level stop flag (call at the start of run_serve)."""
    global _stop_flag
    _stop_flag = False


def _stop_handler(signum, frame):  # noqa: ANN001, ANN002
    """Signal handler: set the stop flag. Minimal work (no I/O); the loop
    does the flush."""
    global _stop_flag
    _stop_flag = True


# How often the loop re-checks the stop flag while waiting between ticks.
# Kept short enough that a signal is honored promptly (~100 ms) without
# busy-spinning the CPU.
_STOP_POLL_S = 0.1


def serve_once_tick(db, config) -> dict:
    """One serve tick: fire the due schedules, audit, and advance.

    Args:
        db: 001 state connection (schedules + audit_runs tables, v2).
        config: the 001 config dict, read fresh on every call (no caching).
            The tick resolves its own qdrant client, embedder, and (optional)
            neo4j driver from config — the same wiring the CLI's ``run``
            command uses — so callers hand it exactly what they hand
            ``run_pipeline``: the resolved config.

            003 multi-user (C-3): for each due schedule, the tick merges the
            schedule owner's ``user_config`` overrides into a **copy** of the
            global config via :func:`merge_user_config` and passes that merged
            config to ``run_pipeline``. The global ``config`` is never mutated
            (SC-003 isolation at the serve level): two owners' merges are
            independent, so alice's cap change never alters bob's run.

    Returns:
        ``{"fired": [schedule id...], "skipped": [schedule id...],
        "queue_depth": int}`` — see module docstring for the semantics.
    """
    now = datetime.now(timezone.utc)
    due = due_schedules(db, now)

    # Resolve the pipeline's dependencies from config, once per tick.
    # (The QdrantClient is a factory — run_pipeline calls it lazily after
    # the prerequisite check, mirroring 001's fail-fast ordering.)
    qdrant = _qdrant_factory(config)
    embedder = _embedder(config)
    neo4j = None  # 002 v1: serve fires use the shared 001 state store;
    # graph writes are a 001 concern (T007 wires the driver if needed).

    fired: list[int] = []
    skipped: list[int] = []

    for schedule in due:
        source = schedule["source"]
        owner = schedule["owner"]
        entry = config["sources"].get(source)
        if entry is None or not entry.get("enabled"):
            # Paused source: do NOT fire-and-advance. No audit row (nothing
            # ran); next_fire_at stays put so the schedule is still due on
            # the next tick and fires when the source is re-enabled.
            skipped.append(schedule["id"])
            continue

        # 003 multi-user (C-3): merge the schedule owner's per-user overrides
        # into a COPY of the global config. The global `config` is never
        # mutated (SC-003): merge_user_config returns a fresh deep copy with
        # the owner's overrides applied. If the owner has no overrides the
        # merge is a no-op (the copy equals the global).
        merged_config = merge_user_config(config, db, owner, source=source)

        fired_at = now
        rows_before = _audit_count(db)
        try:
            # The pipeline writes its own audit row (trigger + scheduled_by
            # passed through); success or not, advance the schedule.
            # R6: pass owner=owner so the pipeline stamps owner + owner_tag
            # on each point payload (per-user scoping, SC-005).
            run_pipeline(
                merged_config, db, qdrant, embedder,
                source_names=[source],
                trigger="schedule",
                scheduled_by=owner,
                owner=owner,
            )
        except Exception:
            # R-07: a failed fire is audited and advanced — never silent,
            # never re-fired. run_pipeline writes its own `failed` audit row
            # (start_audit_run up front + finish on the exception path); if
            # the run died before any audit row was started, backstop with a
            # fresh `failed` row so the fire is always reported.
            if _audit_count(db) == rows_before:
                _audit_failed_run(db, source, owner)
        claim_and_advance(db, schedule["id"], fired_at)
        fired.append(schedule["id"])

    # v1 does not interrupt the tick: every due schedule is either fired
    # or skipped above, so queue_depth is normally 0. It is > 0 only if a
    # non-prerequisite error aborted the whole tick mid-loop.
    queue_depth = len(due) - len(fired) - len(skipped)

    return {"fired": fired, "skipped": skipped, "queue_depth": queue_depth}


def _qdrant_factory(config) -> callable:
    """Zero-arg factory returning the configured Qdrant client (lazy).

    Mirrors the CLI's ``run`` command: the client is only constructed after
    the prerequisite check passes, so a missing prerequisite never pays the
    cost of a network connection.
    """
    url = get(config, "qdrant.url")

    def factory():
        if not url:
            from digital_twins.config.schema import ConfigError
            raise ConfigError(
                "qdrant.url is not set — run init or set KB_QDRANT__URL")
        from qdrant_client import QdrantClient
        return QdrantClient(
            url=url, api_key=get(config, "qdrant.api_key") or None)

    return factory


def _embedder(config):
    """Lazy embedder: the heavy model loads on first call, not at tick start."""
    state = {}

    def embed(texts):
        if "model" not in state:
            from digital_twins.ingest.embedding import load_embedder
            state["model"] = load_embedder(
                get(config, "embedding.model"),
                get(config, "embedding.device") or "auto")
        return state["model"].encode(list(texts)).tolist()

    return embed


def _audit_count(db) -> int:
    """Number of audit_runs rows (to detect whether a run started its row)."""
    return db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]


def _audit_failed_run(db, source: str, owner: str) -> None:
    """Write a ``failed`` audit row for a schedule fire whose run raised.

    run_id is a fresh uuid4 hex (no reuse of the pipeline's run id: the
    pipeline may have died before starting its own audit row, and we must
    not collide with a row it did start).
    """
    run_id = uuid.uuid4().hex
    start_audit_run(db, run_id, trigger="schedule", scheduled_by=owner)
    finish_audit_run(db, run_id, "failed", {source: 0})


# --- run_serve (T007): serve loop — pidfile guard + clean signal shutdown ---

def run_serve(db, config, status_port: int, *, status_server=None,
              tick_seconds: float = TICK_SECONDS,
              max_ticks: int | None = None) -> None:
    """Run the serve loop until a stop signal (or ``max_ticks``).

    Contract (contracts/scheduler.md post-ruling R4 / R-08):
        pidfile guard at ``config["state_dir"]/"serve.lock"`` (R4) ->
        status server if provided -> loop{ fresh-config load();
        serve_once_tick; sleep } until SIGTERM/SIGINT.

    Args:
        db: an open 001 SQLite connection (the caller opens it; run_serve
            closes it on shutdown).
        config: the resolved 001 config dict (used only for the pidfile
            path — ``config["state_dir"]`` — and as the loop's starting
            point; each tick re-loads config fresh via :func:`load_config`,
            so cap/pause changes are honored without restart, SC-005).
        status_port: the port the caller (T008 CLI) used to build
            ``status_server``; run_serve does NOT build the HTTP server
            itself (keeps loop.py testable without sockets).
        status_server: an optional object with ``start()``/``stop()``
            (the 001-pattern ``ThreadingHTTPServer``, T017). Started before
            the first tick, stopped after the last tick / on shutdown.
        tick_seconds: the loop cadence (default :data:`TICK_SECONDS`).
        max_ticks: a documented test affordance — when set, the loop exits
            cleanly after that many ticks, taking the same shutdown path as
            a signal. (Like 001's test-only parameters.)

    Pidfile semantics (R4):
        - If ``serve.lock`` exists and the pid in it is LIVE
          (``os.kill(pid, 0)`` succeeds) -> fail fast with a
          ``SystemExit`` naming the lock path + the live pid. The CLI maps
          this to exit 2. No tick runs; the lock is left untouched.
        - If the pidfile exists but the pid is DEAD (stale) -> reclaim:
          overwrite with our own pid and continue.
        - Otherwise write our own pid.
        - On clean shutdown: remove ``serve.lock``.

    Migrations: run_serve does NOT call ``migrate()`` — the caller (CLI
    pre_command / T008) has already applied migration v2 (ruling R-08).
    Signal handlers do not run migrations.

    Shutdown: stop the status_server (if provided), close the db, remove
    ``serve.lock``. Config errors mid-run (a bad ``kb.local.yml`` edit) are
    not a crash: ``SystemExit`` with the error named, and the shutdown path
    still runs (pidfile removed, db closed).
    """
    lock = Path(config["state_dir"]) / "serve.lock"
    _acquire_pidfile(lock)
    _reset_stop_flag()

    prev_term = signal.signal(signal.SIGTERM, _stop_handler)
    prev_int = signal.signal(signal.SIGINT, _stop_handler)
    try:
        if status_server is not None:
            status_server.start()
        ticks = 0
        while not _stop_flag:
            try:
                fresh_config = load_config()
            except Exception as exc:
                # A config edit mid-run is not a crash: name it, clean
                # shutdown. (The pidfile was already acquired above.)
                raise SystemExit(f"config error: {exc}") from exc
            serve_once_tick(db, fresh_config)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            _wait_for_stop(tick_seconds)
    finally:
        if status_server is not None:
            status_server.stop()
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        _remove_pidfile(lock)
        db.close()


def _wait_for_stop(seconds: float) -> None:
    """Sleep for ``seconds`` but return early the moment the stop flag is
    set. Polls at :data:`_STOP_POLL_S` so a signal is honored within ~100 ms
    without busy-spinning."""
    deadline = time.monotonic() + seconds
    while not _stop_flag:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_STOP_POLL_S, remaining))


def _acquire_pidfile(lock: Path) -> None:
    """Acquire the pidfile guard (R4).

    - Live pid in the lock -> fail fast (``SystemExit`` naming lock + pid).
    - Stale (dead pid) or missing -> write our own pid.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            existing = int(lock.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing = 0
        if existing > 0 and _pid_is_live(existing):
            raise SystemExit(
                f"serve already running: pid {existing} holds {lock} — "
                f"stop it first or remove the lock if it is stale")
        # Stale (dead pid) or unparseable: reclaim below.
    # Documented choice: write directly (not temp+rename). The pidfile is a
    # single small integer; a direct write is atomic enough for the R4 use
    # (a torn read of an integer is caught by the int() parse and treated
    # as stale -> reclaimed). temp+rename would be belt-and-suspenders but
    # adds a temp file to manage on every start.
    lock.write_text(str(os.getpid()), encoding="utf-8")


def _pid_is_live(pid: int) -> bool:
    """True if ``pid`` names a live process (``os.kill(pid, 0)`` succeeds).

    ``pid <= 0`` is not a valid probe (``os.kill(0, 0)`` would signal the
    whole process group) — treat as not-live so the caller reclaims instead
    of blocking on an invalid value.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but is not ours -> treat as live (do not steal it).
        return True
    return True


def _remove_pidfile(lock: Path) -> None:
    """Remove the pidfile on clean shutdown (missing is a no-op)."""
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
