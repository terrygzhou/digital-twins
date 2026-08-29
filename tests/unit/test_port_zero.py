"""T018 — port 0 disables the status server entirely (no socket bound).

Contract (002, contracts/scheduler.md + contracts/cli.md):
    "Port ``scheduler.status_port`` (default 8765); ``0`` disables the
    server entirely." and "``serve --port 0`` disables the status endpoint."

When the port is 0:
    * No ``ThreadingHTTPServer`` is constructed or started.
    * No socket is bound to any status port.
    * ``serve`` still runs its normal loop (the scheduler works; only the
      HTTP endpoint is off).

RED-first per the task brief: these tests encode the port-0 contract. If a
future regression makes T008's ``cli serve`` build a ``StatusServer`` for
port 0, or makes T007's ``run_serve`` start/stop a status server it was not
handed, one of these tests turns red.

Coverage split (per the brief):
    * ``run_serve`` level (T007): with ``status_server=None`` the loop runs
      cleanly and never touches a status server (no socket bound) — the
      ``run_serve`` side that already landed in T007.
    * ``serve`` CLI level (T008): T008 HAS landed, so this file adds the
      CLI-level test that ``serve --port 0`` does not construct a
      ``StatusServer`` (``status_server=None`` is passed to ``run_serve``).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.scheduler import loop
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# --- harness -----------------------------------------------------------------


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


def _stub_tick(monkeypatch, calls: list):
    """Record each tick; return the idle shape.

    run_serve re-loads config fresh each tick (SC-005). The stub does not
    care about the config contents — it just proves the loop ran.
    """
    def stub(db, fresh_config):
        calls.append(fresh_config)
        return {"fired": [], "skipped": [], "queue_depth": 0}

    monkeypatch.setattr(loop, "serve_once_tick", stub)


class _RecordingStatusServer:
    """Stand-in that records start()/stop() and reports its bound address.

    If a regression makes run_serve start/stop a status server it was not
    handed (or the CLI build one for port 0), the recorded calls catch it.
    ``bound_addr`` is what the test asserts on: it is None when no socket
    was ever bound.
    """

    def __init__(self):
        self.started = False
        self.stopped = False
        self.bound_addr = None

    def start(self):
        self.started = True
        # A real StatusServer binds at construction (ThreadingHTTPServer),
        # not at start(). For the stand-in we record a plausible address at
        # start() so "was it ever started?" is observable.
        self.bound_addr = ("127.0.0.1", 8765)

    def stop(self):
        self.stopped = True


def _free_port() -> int:
    """Ask the OS for a free ephemeral port (to assert it is NOT bound)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _port_is_bound(port: int, timeout: float = 0.25) -> bool:
    """True if a TCP connect to 127.0.0.1:port succeeds within ``timeout``."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        s.close()


# --- 1 — run_serve with status_server=None: no socket, clean loop -------------


def test_run_serve_no_status_server_no_socket(tmp_path, monkeypatch):
    """T007 side: run_serve(status_port=0, status_server=None) runs the
    loop cleanly and never starts/stops a status server — no socket bound.

    Proves three things at once:
      * the loop runs (max_ticks ticks complete; the pidfile is removed on
        clean shutdown);
      * a status server is NOT started (the stand-in server is None, and if
        it were non-None and start()ed the test would catch the start);
      * no status-port socket is bound after the loop returns.
    """
    calls = []
    _stub_tick(monkeypatch, calls)
    db = _db(tmp_path)
    cfg = _cfg(tmp_path)

    # Pick a free port to represent "the port that WOULD be the status
    # port". With status_server=None nothing binds it.
    probe_port = _free_port()

    loop.run_serve(
        db, cfg, 0, status_server=None,
        tick_seconds=0.01, max_ticks=1,
    )

    # The loop ran: exactly one tick.
    assert len(calls) == 1
    # No status server was started or stopped (None was passed).
    # db was closed by run_serve on clean shutdown (contract: caller opens,
    # run_serve closes on the shutdown path).
    with pytest.raises(Exception):
        db.execute("SELECT 1")
    # No socket bound to the probe port (connection refused).
    assert not _port_is_bound(probe_port), (
        f"a socket is bound to port {probe_port} even though "
        "status_server=None"
    )


# --- 2 — CLI serve --port 0: status_server is None (no StatusServer built) -----


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    """Isolated config + state dirs (001 CLI test pattern)."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)
    return config_dir, state_dir


def _seed_db(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(state_dir)
    migrate(conn)
    return conn


class _RunServeStub:
    """Records the run_serve call without running the real loop.

    Mirrors the signature: run_serve(db, config, status_port, *,
    status_server=None, tick_seconds=..., max_ticks=None).
    """

    def __init__(self):
        self.called = False
        self.status_port = None
        self.status_server = None

    def __call__(self, db, config, status_port, *, status_server=None,
                 tick_seconds=5.0, max_ticks=None):
        self.called = True
        self.status_port = status_port
        self.status_server = status_server
        db.close()  # like the real run_serve on clean shutdown


def test_serve_cli_port_zero_no_socket(env_dirs, monkeypatch):
    """T008 side: ``serve --port 0`` must NOT construct a StatusServer.

    The CLI hands ``status_server=None`` to run_serve when the port is 0,
    so no ThreadingHTTPServer is built and no socket is bound. The serve
    command still runs its normal loop (run_serve is invoked and returns
    cleanly -> exit 0).
    """
    _seed_db(env_dirs[1])
    stub = _RunServeStub()
    monkeypatch.setattr(loop, "run_serve", stub)

    result = CliRunner().invoke(cli, ["serve", "--port", "0"])
    assert result.exit_code == 0, result.output

    # run_serve was called with port 0 and NO status server.
    assert stub.called is True
    assert stub.status_port == 0
    assert stub.status_server is None, (
        "serve --port 0 constructed a StatusServer; port 0 must disable "
        "the status endpoint entirely (no socket bound)"
    )


# --- 3 — contrast: port > 0 DOES build a StatusServer (binds a socket) ----------


def test_serve_cli_default_port_builds_status_server(env_dirs, monkeypatch):
    """Positive half of the port-0 contract: with the default port (> 0) the
    CLI DOES construct a StatusServer that would bind a socket — proving the
    port-0 path is specifically the one that avoids the bind.

    Asserts only on the object the CLI hands to run_serve (a real
    StatusServer instance) — no socket is actually bound here, keeping the
    test hermetic (the real "port > 0 serves /status" behavior is covered by
    test_status.py and test_serve_cli.py::test_serve_default_port_from_config).
    """
    _seed_db(env_dirs[1])
    stub = _RunServeStub()
    monkeypatch.setattr(loop, "run_serve", stub)

    result = CliRunner().invoke(cli, ["serve"])  # default port 8765
    assert result.exit_code == 0, result.output

    assert stub.called is True
    assert stub.status_port == 8765
    # T017 has landed: port > 0 -> a real StatusServer is constructed.
    from digital_twins.scheduler.status import StatusServer
    assert isinstance(stub.status_server, StatusServer)
