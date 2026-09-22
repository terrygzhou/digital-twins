"""``digital-twins web`` subcommand tests (T017, US1 — FR-013 / C-6).

RED-first per the task brief: these tests fail until T018 lands the
``web`` click subcommand in ``digital_twins/cli.py``.  The subcommand does
not exist yet, so the expected RED failure is click's "No such command"
error — NOT a fixture/setup error.

Design (documented choices):

- **subprocess, not CliRunner.**  The ``web`` subcommand blocks in
  ``serve_forever`` (the contract mirrors 003's ``serve``: a long-running
  server).  The click ``CliRunner`` would hang the test process on that
  blocking call, so each test spawns ``digital-twins web`` in a
  subprocess, drives it, and terminates it cleanly.  (001/003's CLI tests
  use CliRunner only because their commands return; 006's ``web`` command
  is the first blocking CLI surface.)

- **Config isolation.**  The config layer reads env vars first
  (``KB_CONFIG_DIR`` / ``KB_STATE_DIR`` / ``KB_WEB__BIND`` /
  ``KB_WEB__PORT``), so each test points the subprocess at a fresh tmp
  state dir + config dir and overrides ``web.bind``/``web.port`` via env.
  ``pre_command`` migrates the state DB (C-6: the web subcommand reuses it
  for config load + migrations).

- **Scheduler isolation.**  The web subcommand must NOT start the scheduler
  loop and must NOT write the scheduler pidfile (``serve.lock`` — C-6: a
  separate surface from ``serve``).  No scheduler code runs inside this
  test, so the negative assertion is: the subprocess exits cleanly while
  the server is alive, and no ``serve.lock`` is ever created in the state
  dir.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# The repo root (this file is tests/unit/test_web_cli.py): the spawned CLI
# process must be able to ``import digital_twins`` even though it runs with
# a tmp working directory — the worktree root is put on PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# A non-default port so test 1 can distinguish "honored the knob" from
# "fell back to a hard-coded default".  (Default web.port is 8767.)
BIND = "127.0.0.1"
PORT = 8791


# --- harness ---------------------------------------------------------------


def _subprocess_env(tmp_path: Path) -> dict:
    """Isolated env for a spawned CLI: tmp state/config dirs + web knobs."""
    env = dict(os.environ)
    env.update({
        "KB_CONFIG_DIR": str(tmp_path / "config"),
        "KB_STATE_DIR": str(tmp_path / "state"),
        "KB_WEB__BIND": BIND,
        "KB_WEB__PORT": str(PORT),
        # Test-only: let the spawned process import the package from this
        # worktree (it runs with a tmp cwd; the shipped CLI is unmodified).
        "PYTHONPATH": str(_REPO_ROOT),
    })
    # Strip auth-only vars so a parent env can't leak credentials in.
    for var in ("DT_USER_PASSWORD", "DT_PERSONAL_TOKEN",
                "DT_SESSION_TOKEN", "DT_SERVICE_TOKEN"):
        env.pop(var, None)
    return env


class _Server:
    """A spawned ``digital-twins web`` process + readiness/shutdown helpers."""

    def __init__(self, tmp_path: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "digital_twins", "web"],
            cwd=str(tmp_path),
            env=_subprocess_env(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_for_ready(self, timeout_s: float = 10.0) -> None:
        """Poll until the configured address accepts a TCP connection.

        Raises AssertionError if the port is not bound to the configured
        address within ``timeout_s``.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise AssertionError(
                    f"digital-twins web exited early "
                    f"(code {self.proc.returncode}):\n{out}")
            with socket.socket() as probe:
                probe.settimeout(0.25)
                try:
                    probe.connect((BIND, PORT))
                    return
                except OSError:
                    time.sleep(0.1)
        raise AssertionError(
            f"web server did not bind to {BIND}:{PORT} within "
            f"{timeout_s}s")

    def output(self, timeout_s: float = 5.0) -> str:
        """All captured output so far (non-blocking).

        Uses ``select`` on the file descriptor + ``os.read`` for a true
        non-blocking read.  The previous implementation used
        ``proc.stdout.read(4096)`` after ``select`` reported data, but
        ``text=True`` makes ``read`` a buffered call that can block past
        the available data when the child holds the write end open.
        """
        out = ""
        if self.proc.stdout is not None:
            import os as _os
            import select
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                ready, _, _ = select.select(
                    [self.proc.stdout], [], [], 0.05)
                if not ready:
                    break
                try:
                    chunk = _os.read(
                        self.proc.stdout.fileno(), 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk.decode("utf-8", errors="replace")
        return out

    def stop(self) -> None:
        """Terminate the server cleanly and reap it."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


# 1 — the web subcommand binds to the configured address ---------------------


def test_web_subcommand_binds_to_configured_address(tmp_path):
    """FR-013 / C-6: ``digital-twins web`` binds to web.bind/web.port.

    The bind/port are overridden via config/env in a tmp state dir; the
    socket must bind EXACTLY to the configured address — a hard-coded
    default would fail the connect probe on the configured port.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    server = _Server(tmp_path)
    try:
        server.wait_for_ready()  # poll until the socket is up
        # A successful connect above IS the readiness confirmation:
        # the socket is bound to the configured address.
        probe = socket.socket()
        try:
            probe.settimeout(1.0)
            probe.connect((BIND, PORT))
        finally:
            probe.close()
        # The configured address is NOT the built-in default port.
        assert PORT != 8767, "test must use a non-default port"
        # No scheduler pidfile may appear (web is not serve).
        assert not (state_dir / "serve.lock").exists()
    finally:
        server.stop()


# 2 — the web subcommand prints the listening URL ----------------------------


def test_web_subcommand_prints_listening_url(tmp_path):
    """FR-013 / C-6: stdout carries the exact listening URL form.

    The spec's form (plan C-6 / T017):
    ``web: listening on http://<bind>:<port> (UI: http://<bind>:<port>/)``
    """
    server = _Server(tmp_path)
    try:
        server.wait_for_ready()
        out = server.output()
        expected = f"web: listening on http://{BIND}:{PORT}"
        assert expected in out, (
            f"stdout did not contain the listening URL:\n{out}")
        # The full form also names the UI URL.
        assert f"(UI: http://{BIND}:{PORT}/)" in out, (
            f"stdout did not contain the UI URL:\n{out}")
    finally:
        server.stop()


# 3 — the web subcommand does not start the scheduler -------------------------


def test_web_subcommand_does_not_start_scheduler(tmp_path):
    """C-6: the web subcommand is a separate surface from ``serve``.

    It must NOT invoke the scheduler loop and must NOT write the
    scheduler pidfile (``state_dir/serve.lock``).  The server here is
    driven only by the web app (build_web_app + serve_forever); no
    scheduler code path is imported or called.  Assertions:
    - the web server is up and serving on the configured address;
    - the process stays alive while serving (it is NOT doing scheduler
      work that would exit/crash it);
    - no ``serve.lock`` pidfile is ever created in the state dir.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    server = _Server(tmp_path)
    try:
        server.wait_for_ready()
        # Give the (non-existent, or wrongly-attached) scheduler loop a
        # moment to create the pidfile if it were running: it must not.
        for _ in range(20):
            if (state_dir / "serve.lock").exists():
                break
            time.sleep(0.1)
        assert not (state_dir / "serve.lock").exists(), (
            "web subcommand must not write the scheduler pidfile "
            "(serve.lock)")
        # The process is still alive and serving (not a crashed /
        # scheduler-exited process).
        assert server.proc.poll() is None, (
            "web subcommand exited; scheduler lifecycle likely involved")
    finally:
        server.stop()


# 4 — the web subcommand reuses pre_command (migrations) -----------------------


def test_web_subcommand_reuses_pre_command(tmp_path):
    """C-6: the web subcommand runs cli.pre_command (load config +
    complete migrations) before serving.

    ``pre_command`` migrates the state DB when the state dir exists.  A
    fresh tmp state dir is created empty; after ``digital-twins web``
    starts serving, the state DB must exist and carry the migrated v3
    schema (``PRAGMA user_version`` == 3).
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    server = _Server(tmp_path)
    try:
        server.wait_for_ready()
        db_path = state_dir / "state.db"
        assert db_path.exists(), (
            "pre_command must create + migrate the state DB before "
            "the web server starts")
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            version = conn.execute(
                "PRAGMA user_version").fetchone()[0]
            assert version == 3, (
                f"expected migrated v3 schema, got user_version "
                f"{version}")
            # The accounts table exists (part of the migrated schema).
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "accounts" in tables, (
                f"accounts table missing after web start: {tables}")
        finally:
            conn.close()
    finally:
        server.stop()


# --- sanity: the RED reason is "unknown command", not a fixture error --------


def test_web_subcommand_red_failure_reason(tmp_path):
    """Guard for this RED file: if T018 is not yet landed, the failure
    must be click's "No such command" — proving the RED is caused by the
    missing subcommand, not by a broken fixture.

    Once T018 lands, this test PASSES (the subcommand exists), and the
    other four tests in this file verify its behavior.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "digital_twins", "web", "--help"],
        cwd=str(tmp_path),
        env=_subprocess_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        # RED: the subcommand does not exist yet.
        assert "No such command" in (proc.stdout + proc.stderr), (
            f"expected click's 'No such command' for the missing web "
            f"subcommand, got:\n{proc.stdout}\n{proc.stderr}")


# 5 — a busy web port fails fast with a clear message, not a traceback ------


def _reserve_addr(bind: str, port: int):
    """Hold ``bind:port`` with a throwaway socket for the test's duration.

    Returned socket stays open until the test's ``finally`` closes it, so
    ``digital-twins web`` hitting the same address gets ``EADDRINUSE``
    (POSIX) / ``WSAEADDRINUSE`` (Windows).
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind((bind, port))
    holder.listen(1)
    return holder


def test_web_subcommand_fails_fast_on_busy_port(tmp_path):
    """If web.port is already taken, ``digital-twins web`` must fail fast
    with exit code 2 + a human-readable stderr line (port + the web.port
    knob name) — never a raw ``OSError`` / ``Traceback``.

    The pre-fix behavior was a bare
    ``OSError: [Errno 48] Address already in use`` traceback from
    ``socketserver.server_bind``.
    """
    holder = _reserve_addr(BIND, PORT)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "digital_twins", "web"],
            cwd=str(tmp_path),
            env=_subprocess_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 2, (
            f"expected fail-fast exit code 2, got {proc.returncode}:\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert "Traceback" not in proc.stderr, (
            f"raw traceback leaked to stderr:\n{proc.stderr}")
        # Human-readable remediation, naming the address + the knob.
        expected = (f"web: port {PORT} is already in use on {BIND}")
        assert expected in proc.stderr, (
            f"expected remediation line {expected!r} in stderr:\n"
            f"{proc.stderr}")
        assert "web.port" in proc.stderr, (
            f"stderr must name the web.port knob:\n{proc.stderr}")
    finally:
        holder.close()


def test_web_subcommand_free_port_still_starts(tmp_path):
    """Counter-check: when web.port is free, ``digital-twins web`` still
    binds and serves (the fail-fast path must not shadow the happy path).
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    server = _Server(tmp_path)
    try:
        server.wait_for_ready()
        out = server.output()
        assert f"web: listening on http://{BIND}:{PORT}" in out, (
            f"happy-path URL missing from stdout:\n{out}")
        assert server.proc.poll() is None
    finally:
        server.stop()
