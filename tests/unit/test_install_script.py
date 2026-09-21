"""Mocked-exec branch tests for scripts/install-local.sh (Option A installer).

Binding ruling R1 (mirrors bootstrap/uninstall scripts): the script honours
the ``INSTALL_EXEC`` env var — a fake binary prepended to every external
command via the single ``run_cmd()`` wrapper.  The fake logs argv to a JSONL
file (path via ``INSTALL_EXEC_LOG``) and returns canned exit codes.

Because the script discovers Python via ``run_cmd`` (so the fake can stub
the version probe), tests point ``--python`` at the fake binary itself and
hide the real interpreter from PATH.

Each test asserts on the exec log, the subprocess exit code, and emitted
stderr lines.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "install-local.sh"

FAKE_EXEC_PY = """\
#!/usr/bin/env python3
import json, os, sys

log_path = os.environ.get("INSTALL_EXEC_LOG")
if log_path:
    with open(log_path, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")

# Canned: when invoked as a python interpreter asked for its version
# (argv[-1] is a -c code string mentioning version_info), report 3.12.
if len(sys.argv) >= 3 and sys.argv[2] == "-c" and "version_info" in sys.argv[3]:
    print("3.12")
sys.exit(0)
"""


@pytest.fixture
def fake_exec(tmp_path):
    exe = tmp_path / "fakeexec"
    exe.write_text("#!/bin/sh\nexec python3 -c 'import sys; sys.exit(0)'\n" if False else FAKE_EXEC_PY,
                   encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "exec.log"
    home = tmp_path / "home"
    home.mkdir()
    return exe, log, home


def _run(args, fake_exec, extra_env=None):
    exe, log, home = fake_exec
    empty = home / "emptybin"
    empty.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({
        "INSTALL_EXEC": str(exe),
        "INSTALL_EXEC_LOG": str(log),
        "HOME": str(home),
    })
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )
    calls = []
    if log.exists():
        calls = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    return proc, calls


def test_python_not_found_exits_2(fake_exec):
    # No --python, and PATH has no python3/python -> exit 2.
    exe, log, home = fake_exec
    empty = home / "emptybin"; empty.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({"INSTALL_EXEC": str(exe), "INSTALL_EXEC_LOG": str(log), "HOME": str(home)})
    # Hide real interpreters: PATH only has the empty bin dir. bash is found
    # via the explicit "bash" argv[0] we pass to subprocess, and `command -v
    # python3` inside the script uses PATH -> empty -> not found.
    env["PATH"] = str(empty)
    import shutil
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [bash, str(SCRIPT), "--no-setup"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "no Python" in proc.stderr


def test_bad_python_override_exits_2(fake_exec):
    exe, log, home = fake_exec
    # Point at a non-existent interpreter: python_ok fails -> exit 2.
    proc, _ = _run(["--python", "/nonexistent/python", "--no-setup"], fake_exec)
    assert proc.returncode == 2
    assert "not a usable interpreter" in proc.stderr


def test_happy_path_no_setup(fake_exec):
    exe, log, home = fake_exec
    # The fake exec itself stands in as the "python" (it answers the version
    # probe and succeeds at everything else).
    proc, calls = _run(["--python", str(exe), "--no-setup"], fake_exec)
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "venv" in joined
    assert "install" in joined and "digital-twins[mcp]" in joined
    # No setup wizard invocation.
    assert not any("setup" in c for c in calls)


def test_extras_flag_passed_to_pip(fake_exec):
    exe, log, home = fake_exec
    proc, calls = _run(["--python", str(exe), "--no-setup",
                        "--extras", "mcp,local-embedding"], fake_exec)
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "digital-twins[mcp,local-embedding]" in joined


def test_base_install_no_extras(fake_exec):
    exe, log, home = fake_exec
    proc, calls = _run(["--python", str(exe), "--no-setup", "--extras", ""], fake_exec)
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "digital-twins[" not in joined


def test_setup_invoked_with_cloud(fake_exec):
    exe, log, home = fake_exec
    proc, calls = _run(["--python", str(exe), "--cloud"], fake_exec)
    assert proc.returncode == 0
    setup_calls = [c for c in calls if "setup" in c]
    assert setup_calls, "expected a setup invocation"
    assert "--cloud" in setup_calls[0]


def test_setup_skip_services_flag(fake_exec):
    exe, log, home = fake_exec
    proc, calls = _run(["--python", str(exe), "--skip-services"], fake_exec)
    assert proc.returncode == 0
    setup_calls = [c for c in calls if "setup" in c]
    assert setup_calls and "--skip-services" in setup_calls[0]


def test_run_ingest_flag_triggers_run(fake_exec):
    exe, log, home = fake_exec
    proc, calls = _run(["--python", str(exe), "--no-setup", "--run-ingest"], fake_exec)
    assert proc.returncode == 0
    run_calls = [c for c in calls if "run" in c and "--source" in c]
    assert run_calls, "expected a `run --source fs` invocation"
