"""Mocked-exec branch tests for scripts/install.sh (the curl|bash entry point).

Binding ruling R1 (mirrors bootstrap / uninstall / install-local): the script
honours the ``INSTALL_SH_EXEC`` env var — a fake binary prepended to every
external command via the single ``run_cmd()`` wrapper.  The fake logs argv to
a JSONL file (path via ``INSTALL_SH_EXEC_LOG``) and returns canned exit codes.

The remote script additionally self-bootstraps: it checks the ``venv``
module is importable (``python -c 'import venv'``) and, when missing, exits
2 with the OS-specific remediation instead of auto-sudo.  These tests cover
that branch and the rest of the flow.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "install.sh"

# Fake exec: log argv; answer the version probe with 3.12; answer the
# 'import venv' probe by reading a flag file (FAKE_VENV_MISSING) so tests
# can force the "venv module missing" branch; succeed at everything else.
FAKE_EXEC_PY = """#!/bin/sh
# Plain-sh fake: sidesteps the python-shebang argv offset. Every external
# command routes through run_cmd() -> "$INSTALL_SH_EXEC" "$@", so this shell
# script is what actually runs. It logs argv (JSONL), answers the python
# "version" and "import venv" probes, and succeeds at everything else.
log="${INSTALL_SH_EXEC_LOG:-}"
[ -n "$log" ] && printf '%s\n' "$*" >> "$log"

# python -c 'import sys; ...version_info...' -> report 3.12.
for a in "$@"; do
  case "$a" in
    *version_info*) echo "3.12" ;;
  esac
done

# python -c 'import venv' -> honour the FAKE_VENV_MISSING flag file.
for a in "$@"; do
  case "$a" in
    *"import venv"*)
      flag="${FAKE_VENV_MISSING:-}"
      if [ -n "$flag" ] && [ -e "$flag" ]; then
        exit 1
      fi
      ;;
  esac
done
exit 0"""


@pytest.fixture()
def fake_exec(tmp_path):
    exe = tmp_path / "fakeexec"
    exe.write_text(FAKE_EXEC_PY, encoding="utf-8")
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
        "INSTALL_SH_EXEC": str(exe),
        "INSTALL_SH_EXEC_LOG": str(log),
        "HOME": str(home),
        "PATH": str(empty),  # hide real python3/python; the fake is used via --python
    })
    if extra_env:
        env.update(extra_env)
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [bash, str(SCRIPT), "--python", str(exe), *args],
        capture_output=True, text=True, env=env,
    )
    calls = []
    if log.exists():
        # The sh fake logs each call's "$*" (space-joined argv) as one line.
        calls = [l.split() for l in log.read_text().splitlines() if l.strip()]
    return proc, calls


def test_happy_path_no_setup(fake_exec):
    proc, calls = _run(["--no-setup"], fake_exec)
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "venv" in joined
    assert "install" in joined and "digital-twins-kb[mcp]" in joined
    assert not any("setup" in c for c in calls)


def test_extras_forwarded(fake_exec):
    proc, calls = _run(["--no-setup", "--extras", "mcp,local-embedding"], fake_exec)
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "digital-twins-kb[mcp,local-embedding]" in joined


def test_base_install_no_extras(fake_exec):
    proc, calls = _run(["--no-setup", "--extras", ""], fake_exec)
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "digital-twins-kb[" not in joined


def test_setup_cloud_flag(fake_exec):
    proc, calls = _run(["--cloud"], fake_exec)
    assert proc.returncode == 0
    setup_calls = [c for c in calls if "setup" in c]
    assert setup_calls and "--cloud" in setup_calls[0]


def test_venv_module_missing_exits_2(fake_exec):
    # Force the 'import venv' probe to fail via the flag file.
    exe, log, home = fake_exec
    flag = home / "venv-missing"
    flag.write_text("1")
    proc, _ = _run(["--no-setup"], fake_exec,
                   extra_env={"FAKE_VENV_MISSING": str(flag)})
    assert proc.returncode == 2
    assert "venv" in proc.stderr and "python3-venv" in proc.stderr


def test_dist_name_override(fake_exec):
    proc, calls = _run(["--no-setup"], fake_exec,
                       extra_env={"INSTALL_SH_DIST": "my-custom-dist"})
    assert proc.returncode == 0
    joined = " ".join(" ".join(c) for c in calls)
    assert "my-custom-dist[mcp]" in joined
