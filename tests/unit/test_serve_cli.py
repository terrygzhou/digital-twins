"""serve command tests (T008, US1 scenario 3 — idle behavior).

RED-first per the task brief: these tests fail until cli.py lands the
``serve`` command. The CLI test pattern matches 001's style (CliRunner +
env-var isolation + health monkeypatching, as in test_init.py /
test_validate.py).

The stop-after-N mechanism: since run_serve has a ``max_ticks`` test
affordance from T007, the CLI test monkeypatches
``digital_twins.scheduler.loop.run_serve`` to a recording stub that
accepts ``max_ticks`` — this proves the CLI WOULD have called run_serve
with the right arguments without actually running the serve loop.
Documented choice: stubbing run_serve (rather than running it for real)
is the least-invasive path; the real loop behavior is already covered by
test_run_serve.py (T007).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from digital_twins import health
from digital_twins.cli import cli
from digital_twins.scheduler import loop
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# --- harness ---------------------------------------------------------------


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    """Isolated config + state dirs (001 CLI test pattern)."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)
    return config_dir, state_dir


def _stub_health(monkeypatch, ok=True):
    """Stub 001 health checks (same pattern as test_init.py)."""

    def _run(cfg):
        return [
            health.HealthResult(ep, ok, "ok" if ok else "down", "")
            for ep in ("qdrant", "neo4j", "llm")
        ]

    monkeypatch.setattr(health, "run_health_checks", _run)


def _seed_db(state_dir: Path):
    """Create + migrate the state DB (no schedules = idle)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(state_dir)
    migrate(conn)
    return conn


class _RunServeStub:
    """Records the call to run_serve without running the real loop.

    Accepts the same signature as loop.run_serve:
    run_serve(db, config, status_port, *, status_server=None,
              tick_seconds=..., max_ticks=None).
    """

    def __init__(self):
        self.called = False
        self.db = None
        self.config = None
        self.status_port = None
        self.status_server = None
        self.tick_seconds = None
        self.max_ticks = None

    def __call__(
        self, db, config, status_port, *, status_server=None,
        tick_seconds=5.0, max_ticks=None,
    ):
        self.called = True
        self.db = db
        self.config = config
        self.status_port = status_port
        self.status_server = status_server
        self.tick_seconds = tick_seconds
        self.max_ticks = max_ticks
        # Close the db like the real run_serve would on clean shutdown.
        db.close()


def _stub_run_serve(monkeypatch):
    stub = _RunServeStub()
    monkeypatch.setattr(loop, "run_serve", stub)
    return stub


def _audit_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]


# 1 — serve command exists and idles with zero schedules --------------------


def test_serve_command_exists_and_idle(env_dirs, monkeypatch):
    """US1 scenario 3: serve with zero schedules starts, idles, and shuts
    down cleanly without audit spam.

    The serve command must:
    - load config (pre_command already did; the command re-loads or reuses)
    - determine status_port (default from scheduler.status_port knob)
    - run 001 health preconditions (stubbed ok here)
    - connect + migrate the db
    - call run_serve(db, config, status_port, status_server=None)
      (port 0 -> no status server)
    - exit cleanly (run_serve returns; the command returns -> exit 0)
    - write ZERO audit rows (idle = no spam)
    """
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    stub = _stub_run_serve(monkeypatch)

    # Seed the DB so pre_command's migrate has a target; no schedules = idle.
    _seed_db(state_dir)

    result = CliRunner().invoke(cli, ["serve", "--port", "0"])
    assert result.exit_code == 0, result.output

    # run_serve was called with the right arguments
    assert stub.called is True
    assert stub.status_port == 0
    assert stub.status_server is None  # port 0 -> no status server
    # config was passed through (state_dir matches our isolated dir)
    assert stub.config is not None
    assert stub.config["state_dir"] == str(state_dir)

    # idle: zero audit rows
    db = connect(state_dir)
    try:
        assert _audit_count(db) == 0
    finally:
        db.close()


# 2 — --port 0 disables the status server ------------------------------------


def test_serve_port_zero_disables_status_server(env_dirs, monkeypatch):
    """--port 0: status_server must be None (no socket bound).

    Asserts the status_server param is None when port=0, confirming no
    ThreadingHTTPServer is constructed or started.
    """
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    stub = _stub_run_serve(monkeypatch)
    _seed_db(state_dir)

    result = CliRunner().invoke(cli, ["serve", "--port", "0"])
    assert result.exit_code == 0, result.output
    assert stub.status_port == 0
    assert stub.status_server is None


def test_serve_default_port_from_config(env_dirs, monkeypatch):
    """No --port flag: status_port falls back to scheduler.status_port knob.

    The built-in default is 8765 (config/schema.py DEFAULTS).
    """
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    stub = _stub_run_serve(monkeypatch)
    _seed_db(state_dir)

    result = CliRunner().invoke(cli, ["serve"])
    assert result.exit_code == 0, result.output
    assert stub.status_port == 8765
    # T017 not landed yet: the status server import fails -> status_server is
    # None (lazy import with try/except ImportError).
    assert stub.status_server is None


# 3 — second instance fails fast on live pidfile -----------------------------


def test_serve_second_instance_fails_fast(env_dirs, monkeypatch):
    """R4: serve.lock holds a LIVE pid -> serve exits 2, stderr names the
    lock + the live pid.

    Pre-write serve.lock with our own (live) pid, then invoke serve.
    run_serve raises SystemExit naming the lock + pid; the CLI must map
    this to exit 2.
    """
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    _seed_db(state_dir)

    # Pre-write the pidfile with a LIVE pid (our own).
    lock = state_dir / "serve.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")

    result = CliRunner().invoke(cli, ["serve", "--port", "0"])
    assert result.exit_code == 2, result.output
    # stderr names the lock + live pid
    assert "serve.lock" in result.output
    assert str(os.getpid()) in result.output


# 4 — missing/unreachable endpoint exits 2 -----------------------------------


def test_serve_missing_endpoint_exit_2(env_dirs, monkeypatch):
    """001 health precondition: a required endpoint (qdrant) unreachable
    -> serve exits 2 with a named error.

    Stub health.run_health_checks to report qdrant as down. The serve
    command must check health BEFORE calling run_serve and exit 2.
    """
    config_dir, state_dir = env_dirs
    _seed_db(state_dir)

    # Health check: qdrant down, others ok.
    monkeypatch.setattr(health, "run_health_checks", lambda cfg: [
        health.HealthResult(
            "qdrant", False,
            "qdrant.url is not configured",
            "set qdrant.url in kb.local.yml (env: KB_QDRANT__URL), "
            "then re-run init/validate",
        ),
        health.HealthResult("neo4j", True, "ok"),
        health.HealthResult("llm", True, "ok"),
    ])

    stub = _stub_run_serve(monkeypatch)
    result = CliRunner().invoke(cli, ["serve", "--port", "0"])
    assert result.exit_code == 2, result.output
    # The named error should appear
    assert "qdrant" in result.output
    # run_serve must NOT have been called (health gate fails before it)
    assert stub.called is False
