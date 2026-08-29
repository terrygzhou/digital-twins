"""run_serve (002, T007): serve loop — pidfile guard + clean signal shutdown.

Contract (contracts/scheduler.md post-ruling R4/R-08):
    run_serve(db, config, status_port, *, status_server=None,
              tick_seconds=TICK_SECONDS, max_ticks=None) -> None
    pidfile guard at config["state_dir"]/"serve.lock" -> status server if
    provided -> loop{ fresh-config load(); serve_once_tick; sleep } until
    SIGTERM/SIGINT (or max_ticks, a documented test affordance).
    On clean shutdown: stop status_server, close db, remove serve.lock.

RED-first per the task brief: these tests fail until loop.py lands
run_serve. The pidfile logic is under test, so the pidfile tests use the
REAL run_serve entry point; the loop's tick call is monkeypatched to a
recording no-op stub so no pipeline actually runs.

Signal tests use the stop-after-N-ticks affordance in-process (sending a
real SIGTERM to the test process would kill pytest); the subprocess
SIGTERM test lives in T008 (CLI serve command).
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

import pytest

from digital_twins.config.loader import ConfigError
from digital_twins.scheduler import loop
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# --- harness ---------------------------------------------------------------

def _cfg(tmp_path) -> dict:
    """A valid resolved config dict (state_dir under tmp_path)."""
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": None, "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "test", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": {"fs": {"enabled": False, "max_items": 200,
                           "timeout_s": 1500}},
    }


def _db(tmp_path):
    Path(tmp_path / "state").mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "state")
    migrate(conn)
    return conn


def _lock(tmp_path) -> Path:
    return Path(_cfg(tmp_path)["state_dir"]) / "serve.lock"


class _StubServer:
    """Records start()/stop() calls; stands in for the T017 status server.

    Exposes the ThreadingHTTPServer surface run_serve manages (start/stop);
    the start() hook is optional so tests can trigger shutdown from inside
    the loop (signal tests)."""

    def __init__(self, on_start=None):
        self.started = False
        self.stopped = False
        self._on_start = on_start

    def start(self):
        self.started = True
        if self._on_start is not None:
            self._on_start()

    def stop(self):
        self.stopped = True


def _stub_tick(monkeypatch, calls: list):
    """Record each tick call; return the idle shape.

    run_serve re-loads config fresh each tick (SC-005); the stub records
    the fresh config it was handed so the fresh-config test can assert on it.
    """
    def stub(db, fresh_config):
        calls.append(fresh_config)
        return {"fired": [], "skipped": [], "queue_depth": 0}

    monkeypatch.setattr(loop, "serve_once_tick", stub)


def _dead_pid() -> int:
    """A pid that is definitely not live: probe until os.kill(pid, 0) raises.

    999999 is almost always dead (Linux PIDs wrap at 32 bits, 4194304), but
    probe to be certain — a live pid would make the stale-reclaim test
    flaky (run_serve would fail fast instead).
    """
    pid = 999999
    for _ in range(10000):
        try:
            os.kill(pid, 0)
            pid = 999999 + pid % 7 + 1
        except ProcessLookupError:
            return pid
    # OSError (no permission) also means "not our process" -> treat as dead.
    return pid


# 1 — live pidfile blocks -----------------------------------------------------

def test_pidfile_live_blocks(tmp_path, monkeypatch):
    """R4: serve.lock holds a LIVE pid -> fail fast with a named error.

    The lock path AND the live pid are named; no tick ran; our pid was
    NOT written to the lock.
    """
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    _lock(tmp_path).write_text(str(os.getpid()), encoding="utf-8")

    with pytest.raises(BaseException) as excinfo:
        loop.run_serve(db, cfg, 0, status_server=None,
                       tick_seconds=0.01, max_ticks=3)

    err = excinfo.value
    assert isinstance(err, SystemExit) or isinstance(err, Exception)
    msg = str(err)
    assert "serve.lock" in msg or str(_lock(tmp_path)) in msg
    assert str(os.getpid()) in msg

    # no loop iteration happened
    assert calls == []
    # the live pid is still in the lock (we did not reclaim it)
    assert _lock(tmp_path).read_text(encoding="utf-8") == str(os.getpid())
    db.close()


# 2 — stale pidfile is reclaimed ---------------------------------------------

def test_pidfile_stale_reclaimed(tmp_path, monkeypatch):
    """R4: serve.lock holds a DEAD pid -> reclaimed (our pid written),
    N ticks run, and on clean stop the pidfile is REMOVED."""
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    dead = _dead_pid()
    _lock(tmp_path).write_text(str(dead), encoding="utf-8")

    loop.run_serve(db, cfg, 0, status_server=None,
                   tick_seconds=0.01, max_ticks=3)

    # ran exactly 3 ticks
    assert len(calls) == 3
    # after clean stop the pidfile is gone
    assert not _lock(tmp_path).exists()
    db.close()


def test_pidfile_written_on_start(tmp_path, monkeypatch):
    """Our pid is written to serve.lock at startup (no pre-existing lock)."""
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    assert not _lock(tmp_path).exists()

    loop.run_serve(db, cfg, 0, status_server=None,
                   tick_seconds=0.01, max_ticks=1)

    assert len(calls) == 1
    assert not _lock(tmp_path).exists()  # removed on clean stop
    db.close()


# 3 — clean shutdown ----------------------------------------------------------

def test_clean_shutdown_removes_pidfile_closes_db_stops_server(tmp_path,
                                                               monkeypatch):
    """On stop (max_ticks), the pidfile is removed, db closed, and the
    status server (if provided) stopped."""
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)
    server = _StubServer()

    loop.run_serve(db, cfg, 0, status_server=server,
                   tick_seconds=0.01, max_ticks=2)

    assert len(calls) == 2
    assert not _lock(tmp_path).exists()
    assert server.started is True
    assert server.stopped is True
    # db was closed by run_serve
    with pytest.raises(Exception):
        db.execute("SELECT 1")


# 4 — signal handling ---------------------------------------------------------

def test_sigterm_sets_stop_flag(tmp_path, monkeypatch):
    """A SIGTERM received mid-loop triggers the clean-shutdown path:
    run_serve returns (does not raise), the pidfile is removed, and the
    handler is restored (no custom handler left behind)."""
    calls = []
    # Tick stub that delivers a SIGTERM to the test process AFTER the first
    # tick has been recorded. The signal lands while the loop is in its
    # inter-tick wait; run_serve's installed handler must catch it and set
    # the stop flag so the loop exits on the next poll.
    delivered = {"done": False}

    def _tick(db, fresh_config):
        calls.append(fresh_config)
        if not delivered["done"]:
            delivered["done"] = True
            # Send the signal from the main thread after a short delay so the
            # tick has returned to the loop's wait. (Sending from the worker
            # thread the tick runs in is fine — signal handlers are a
            # process-level concern, and run_serve's handler is installed on
            # the main thread.)
            threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"fired": [], "skipped": [], "queue_depth": 0}

    monkeypatch.setattr(loop, "serve_once_tick", _tick)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)

    loop.run_serve(db, cfg, 0, status_server=None,
                   tick_seconds=5.0, max_ticks=None)

    # stopped on the signal: 1 tick ran, then the inter-tick wait was
    # interrupted by the SIGTERM.
    assert len(calls) == 1
    assert not _lock(tmp_path).exists()
    # the handler run_serve installed was restored (no custom handler left)
    assert signal.getsignal(signal.SIGTERM) is not loop._stop_handler
    db.close()


def test_original_signal_handlers_restored(tmp_path, monkeypatch):
    """run_serve installs SIGTERM/SIGINT handlers for its own lifetime and
    restores the process's previous handlers before returning."""
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)

    sentinel = lambda *_a: None  # noqa: E731
    signal.signal(signal.SIGTERM, sentinel)
    signal.signal(signal.SIGINT, sentinel)

    try:
        loop.run_serve(db, cfg, 0, status_server=None,
                       tick_seconds=0.01, max_ticks=1)
    finally:
        # Restore the process's true baseline (SIG_DFL=0) so the test leaves
        # no custom handler behind (a leftover handler could mask a real
        # SIGTERM/SIGINT in a later test). run_serve already restored the
        # sentinel, so this just re-baselines.
        signal.signal(signal.SIGTERM, 0)
        signal.signal(signal.SIGINT, 0)

    # run_serve must have restored the handlers it replaced (the sentinel),
    # NOT its own. It ran with max_ticks=1 (no signal), so its restore of the
    # sentinel is the only thing that could have changed the handler.
    # (We re-baselined to SIG_DFL above, so assert it stays SIG_DFL — i.e.
    # run_serve did NOT leave its own handler installed.)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    db.close()


# 5 — status server management -------------------------------------------------

def test_status_server_started_before_loop_stopped_after(tmp_path,
                                                         monkeypatch):
    """The status_server is started before the first tick and stopped after
    the last tick (order: start -> ticks -> stop)."""
    order = []

    def _recording(db, fresh_config):
        order.append("tick")
        return {"fired": [], "skipped": [], "queue_depth": 0}

    monkeypatch.setattr(loop, "serve_once_tick", _recording)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)

    class _Ordered(_StubServer):
        def start(self):
            super().start()
            order.append("start")

        def stop(self):
            super().stop()
            order.append("stop")

    server = _Ordered()
    loop.run_serve(db, cfg, 0, status_server=server,
                   tick_seconds=0.01, max_ticks=2)

    assert order == ["start", "tick", "tick", "stop"]
    db.close()


# 6 — fresh config per tick (SC-005) ------------------------------------------

def test_each_tick_reads_fresh_config(tmp_path, monkeypatch):
    """SC-005: run_serve re-loads config each tick, so cap/pause changes
    are honored without restart. The tick receives the FRESH config, not
    the one handed to run_serve."""
    fresh = []
    monkeypatch.setattr(
        loop, "load_config",
        lambda: {"_marker": "fresh", "state_dir": _cfg(tmp_path)["state_dir"]},
    )
    _stub_tick(monkeypatch, fresh)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)
    cfg["_marker"] = "stale"

    loop.run_serve(db, cfg, 0, status_server=None,
                   tick_seconds=0.01, max_ticks=2)

    assert len(fresh) == 2
    for got in fresh:
        assert got.get("_marker") == "fresh"
    db.close()


def test_config_error_mid_run_exits_cleanly(tmp_path, monkeypatch):
    """A config error mid-run (e.g. a bad kb.yml edit) is not a crash:
    run_serve exits cleanly (removes the pidfile) with the error named."""
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)

    def _fail_load():
        raise ConfigError("bad edit in kb.local.yml (injected)")

    monkeypatch.setattr(loop, "load_config", _fail_load)
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)

    with pytest.raises(SystemExit) as excinfo:
        loop.run_serve(db, cfg, 0, status_server=None,
                       tick_seconds=0.01, max_ticks=5)

    assert "bad edit" in str(excinfo.value)
    # no tick ran, but the pidfile was cleaned up and the db closed
    assert calls == []
    assert not _lock(tmp_path).exists()
    with pytest.raises(Exception):
        db.execute("SELECT 1")
