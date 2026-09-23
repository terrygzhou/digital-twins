"""T001 (015 US1): mocked-exec branch tests for scripts/uninstall-local.sh.

Binding ruling R1: the script routes every external command through a
single ``run_cmd()`` wrapper, which honours the env var ``UNINSTALL_EXEC``.
These tests set ``UNINSTALL_EXEC`` to a Python fake that:

* appends its argv to a JSONL log file (path via ``UNINSTALL_EXEC_LOG``), and
* returns canned stdout / exit codes keyed on the first argv element.

Canned behaviour is driven by a small JSON config the test writes per
branch: ``FAKE_EXEC_CONFIG`` points at a JSON file with keys::

    {"docker": <exit_code_or_dict>,
     "pip": {"exit": 0, "stdout": "..."},
     "uv": {"exit": 0, "stdout": "..."},
     "rm": {"exit": 0},
     ...}

Keyed on the *first* argv element (the command name).  For ``docker`` the
config may carry a ``docker_args`` dict mapping an argv-prefix to a specific
canned response (e.g. ``{"compose": {"exit": 0, "stdout": ""}}``).

Each branch test asserts on:
* the exec log (JSONL: list of argv lists)
* the exit code of the script subprocess
* emitted lines on stdout/stderr (remediation messages, SKIPPED, etc.)

The script mirrors the ``BOOTSTRAP_EXEC`` pattern from
``tests/unit/test_bootstrap_script.py``: the fake exec intercepts every
external call, so tests never touch real docker/pip/uv/rm.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "uninstall-local.sh"

# ---------------------------------------------------------------------------
# Fake exec (UNINSTALL_EXEC) — written to a tmp dir by the fixture
# ---------------------------------------------------------------------------

FAKE_EXEC_PY = """\
#!/usr/bin/env python3
\"\"\"UNINSTALL_EXEC fake: log argv, return canned stdout/exit from config.\"\"\"
import json, os, sys

log_path = os.environ.get("UNINSTALL_EXEC_LOG")
cfg_path = os.environ.get("FAKE_EXEC_CONFIG")

# Log the call
if log_path:
    with open(log_path, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")

# Load config
cfg = {}
if cfg_path:
    with open(cfg_path) as f:
        cfg = json.load(f)

cmd = sys.argv[1] if len(sys.argv) > 1 else ""

# Look up canned response
entry = cfg.get(cmd)
if entry is None:
    # Unknown command: pretend success
    sys.exit(0)

if isinstance(entry, int):
    sys.exit(entry)

# entry is a dict: {"exit": N, "stdout": "...", "stderr": "..."}
if isinstance(entry, dict):
    # For docker, also check docker_args for prefix-specific responses
    if cmd == "docker":
        args_prefix = " ".join(sys.argv[2:4])
        for prefix, sub in entry.get("docker_args", {}).items():
            if args_prefix.startswith(prefix):
                entry = sub
                break
    sys.stdout.write(entry.get("stdout", ""))
    if entry.get("stderr"):
        sys.stderr.write(entry["stderr"])
    sys.exit(entry.get("exit", 0))

sys.exit(0)
"""


@pytest.fixture
def fake_exec(tmp_path: Path) -> Path:
    """Write the fake exec script and return its path."""
    fake = tmp_path / "fake_exec.py"
    fake.write_text(FAKE_EXEC_PY, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


@pytest.fixture
def exec_log(tmp_path: Path) -> Path:
    """JSONL log file for exec calls."""
    return tmp_path / "exec_log.jsonl"


@pytest.fixture
def fake_config(tmp_path: Path) -> Path:
    """JSON config file path for canned responses."""
    return tmp_path / "fake_config.json"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """KB_CONFIG_DIR target — the config dir the script would remove."""
    d = tmp_path / "digital-twins"
    d.mkdir()
    return d


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """KB_STATE_DIR target — the state dir the script would remove."""
    d = tmp_path / ".digital-twins"
    d.mkdir()
    return d


@pytest.fixture
def compose_file(tmp_path: Path) -> Path:
    """A docker-compose.yml the script will find in CWD."""
    p = tmp_path / "docker-compose.yml"
    p.write_text("services: {}\n", encoding="utf-8")
    return p


def _write_config(fake_config: Path, cfg: dict) -> None:
    fake_config.write_text(json.dumps(cfg), encoding="utf-8")


def _exec_log_entries(exec_log: Path) -> list[list[str]]:
    if not exec_log.exists():
        return []
    entries = []
    for line in exec_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _run_script(
    fake_exec: Path,
    exec_log: Path,
    fake_config: Path,
    config_dir: Path,
    state_dir: Path,
    compose_file: Path,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the uninstall script with the fake exec injected."""
    env = {
        **os.environ,
        "UNINSTALL_EXEC": str(fake_exec),
        "UNINSTALL_EXEC_LOG": str(exec_log),
        "FAKE_EXEC_CONFIG": str(fake_config),
        "KB_CONFIG_DIR": str(config_dir),
        "KB_STATE_DIR": str(state_dir),
        # Point HOME at a tmp dir so the script's DEFAULT_CONFIG_DIR /
        # DEFAULT_STATE_DIR don't touch the real filesystem.
        "HOME": str(exec_log.parent),
    }
    # Clear KB_CONFIG_DIR / KB_STATE_DIR from real env, then re-set to tmp.
    env.pop("KB_CONFIG_DIR", None)
    env.pop("KB_STATE_DIR", None)
    env["KB_CONFIG_DIR"] = str(config_dir)
    env["KB_STATE_DIR"] = str(state_dir)
    if extra_env:
        env.update(extra_env)
    # The script looks for docker-compose.yml in CWD; run from the tmp dir
    # that contains it.
    cmd = [str(SCRIPT)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        timeout=30,
        cwd=str(compose_file.parent),
    )


# ---------------------------------------------------------------------------
# Branch tests
# ---------------------------------------------------------------------------


def test_no_compose_file_no_pip_no_data_exit_0(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """Clean host: no compose file, no pip dists, no data dirs → exit 0,
    nothing removed, everything absent or kept."""
    _write_config(
        fake_config,
        {
            # pip show returns 1 for both dists (not installed)
            "pip": 1,
            # pipx list returns 1 (not managed by pipx)
            "pipx": 1,
            # uv pip show returns 1 (no uv venv)
            "uv": 1,
        },
    )
    # Run from a dir WITHOUT docker-compose.yml
    empty_cwd = config_dir  # a plain dir, no compose file
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        extra_env={"KB_CONFIG_DIR": str(config_dir), "KB_STATE_DIR": str(state_dir)},
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0 on clean host, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "absent" in combined.lower(), (
        f"expected absent: line; got: {combined!r}"
    )
    # No docker calls (compose file missing → skipped before docker compose down)
    entries = _exec_log_entries(exec_log)
    docker_calls = [e for e in entries if e and e[0] == "docker"]
    assert not docker_calls, f"no docker calls expected on clean host: {docker_calls}"


def test_pipx_managed_reported_not_removed(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """pipx-managed install: reported, not removed; exit 0."""
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "pip": 1,
            "pipx": {"exit": 0, "stdout": "digital-twins (0.11.0)"},
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "pipx" in combined.lower(), (
        f"expected pipx remediation line; got: {combined!r}"
    )
    assert "pipx uninstall digital-twins" in combined, (
        f"expected 'pipx uninstall digital-twins' remediation; got: {combined!r}"
    )
    # No pip uninstall call (pipx path short-circuits before pip uninstall)
    entries = _exec_log_entries(exec_log)
    pip_uninstall = [e for e in entries if e and e[0] == "pip" and "uninstall" in e]
    assert not pip_uninstall, f"pip uninstall should not be called for pipx installs: {pip_uninstall}"


def test_system_pip_uninstall_success(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """System pip install: pip uninstall succeeds; exit 0."""
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "pip": {"exit": 0, "stdout": "Successfully uninstalled digital-twins-0.11.0"},
            "pipx": 1,
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--force"],  # skip all confirmations
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    pip_uninstall = [e for e in entries if e and e[0] == "pip" and "uninstall" in e]
    assert len(pip_uninstall) == 1, (
        f"expected exactly 1 pip uninstall call; got: {pip_uninstall}"
    )
    # The uninstall call must carry both dist names
    assert "digital-twins" in pip_uninstall[0], (
        f"pip uninstall must carry digital-twins: {pip_uninstall[0]}"
    )
    assert "digital-twins-kb" in pip_uninstall[0], (
        f"pip uninstall must carry digital-twins-kb: {pip_uninstall[0]}"
    )


def test_venv_pip_uninstall(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file, tmp_path
):
    """Venv-based install: venv's own pip is used for uninstall; exit 0."""
    # Create a fake venv dir with an executable pip
    venv_dir = state_dir / ".venv"
    venv_dir.mkdir(parents=True)
    venv_pip = venv_dir / "bin" / "pip"
    venv_pip.parent.mkdir(parents=True, exist_ok=True)
    venv_pip.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    venv_pip.chmod(venv_pip.stat().st_mode | stat.S_IEXEC)
    # Also create venv python (for the uv path check)
    venv_py = venv_dir / "bin" / "python"
    venv_py.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    venv_py.chmod(venv_py.stat().st_mode | stat.S_IEXEC)

    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "pip": 1,  # system pip: not installed
            "pipx": 1,
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--force"],
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    # The venv pip path should have been used (the first element of the
    # call will be the venv pip binary path, not bare "pip")
    venv_pip_calls = [e for e in entries if e and "pip" in e[0] and "uninstall" in e]
    assert len(venv_pip_calls) == 1, (
        f"expected exactly 1 venv-pip uninstall call; got: {venv_pip_calls}"
    )
    assert str(venv_pip) in " ".join(venv_pip_calls[0]), (
        f"venv pip path not in call: {venv_pip_calls[0]}"
    )


def test_tear_down_volumes_success(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--tear-down-volumes with docker compose down -v succeeding: exit 0."""
    _write_config(
        fake_config,
        {
            "docker": {
                "exit": 0,
                "stdout": "",
                "docker_args": {
                    "compose": {"exit": 0, "stdout": "Container stopped"},
                },
            },
            "pip": 1,
            "pipx": 1,
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--tear-down-volumes", "--force"],
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    down_v_calls = [
        e for e in entries if e and e[0] == "docker" and "down" in e and "-v" in e
    ]
    assert len(down_v_calls) == 1, (
        f"expected exactly 1 'docker compose down -v' call; got: {down_v_calls}"
    )


def test_tear_down_volumes_fail_without_force_exit_2(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--tear-down-volumes with down -v failing and no --force: exit 2."""
    _write_config(
        fake_config,
        {
            "docker": {
                "exit": 1,
                "stdout": "",
                "stderr": "Error response from daemon: volume not found",
                "docker_args": {
                    "compose": {"exit": 1, "stdout": "", "stderr": "volume not found"},
                },
            },
            "pip": 1,
            "pipx": 1,
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--tear-down-volumes"],
        stdin="y\n",
    )
    assert result.returncode == 2, (
        f"expected exit 2 (volume-identification failure), got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )


def test_tear_down_volumes_fail_with_force_continues(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--tear-down-volumes with down -v failing and --force: continues,
    reports in kept:, exit 0."""
    _write_config(
        fake_config,
        {
            "docker": {
                "exit": 1,
                "stdout": "",
                "stderr": "Error response from daemon: volume not found",
                "docker_args": {
                    "compose": {"exit": 1, "stdout": "", "stderr": "volume not found"},
                },
            },
            "pip": 1,
            "pipx": 1,
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--tear-down-volumes", "--force"],
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0 under --force, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "kept" in combined.lower(), (
        f"expected kept: line reporting the failure; got: {combined!r}"
    )
    assert "down -v failed" in combined, (
        f"expected 'down -v failed' in kept: line; got: {combined!r}"
    )


def test_skip_docker_flag(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--skip-docker: no docker compose down calls at all; exit 0."""
    _write_config(
        fake_config,
        {
            "pip": 1,
            "pipx": 1,
            "uv": 1,
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--skip-docker"],
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    docker_calls = [e for e in entries if e and e[0] == "docker"]
    assert not docker_calls, (
        f"--skip-docker must not call docker: {docker_calls}"
    )
    combined = result.stdout + result.stderr
    assert "skipped" in combined.lower(), (
        f"expected 'skipped' in output; got: {combined!r}"
    )


def test_remove_data_rm_success(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--remove-data with rm -rf succeeding: both dirs removed; exit 0."""
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "pip": 1,
            "pipx": 1,
            "uv": 1,
            "rm": {"exit": 0, "stdout": ""},
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--remove-data", "--force"],
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    rm_calls = [e for e in entries if e and e[0] == "rm"]
    assert len(rm_calls) == 2, (
        f"expected 2 rm -rf calls (config + state); got: {rm_calls}"
    )
    assert len(rm_calls) == 2, (
        f"expected 2 rm -rf calls (config + state); got: {rm_calls}"
    )


def test_remove_data_rm_fail_without_force_exit_1(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--remove-data with rm failing and no --force: exit 1 + remediation."""
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "pip": 1,
            "pipx": 1,
            "uv": 1,
            "rm": {"exit": 1, "stdout": "", "stderr": "Permission denied"},
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--remove-data"],
        stdin="y\ny\n",
    )
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "remediat" in combined.lower() or "rm -rf" in combined, (
        f"expected remediation; got: {combined!r}"
    )


def test_remove_data_rm_fail_with_force_continues(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--remove-data with rm failing and --force: continues, exit 0,
    failure reported in kept:."""
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "pip": 1,
            "pipx": 1,
            "uv": 1,
            "rm": {"exit": 1, "stdout": "", "stderr": "Permission denied"},
        },
    )
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--remove-data", "--force"],
        stdin="",
    )
    assert result.returncode == 0, (
        f"expected exit 0 under --force, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "rm failed" in combined, (
        f"expected 'rm failed' in kept: line; got: {combined!r}"
    )


def test_unknown_flag_exit_1(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """Unknown argument → exit 1 with remediation."""
    _write_config(fake_config, {})
    result = _run_script(
        fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file,
        args=["--bogus"],
        stdin="",
    )
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "unknown argument" in combined.lower(), (
        f"expected 'unknown argument' in output; got: {combined!r}"
    )


def test_help_lists_flags(
    fake_exec, exec_log, fake_config, config_dir, state_dir, compose_file
):
    """--help must list all five flags and exit 0."""
    result = subprocess.run(
        [str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "UNINSTALL_EXEC": ""},
    )
    assert result.returncode == 0, f"--help exit {result.returncode}: {result.stderr}"
    out = result.stdout
    for flag in (
        "--tear-down-volumes",
        "--remove-data",
        "--force",
        "--skip-docker",
        "--help",
    ):
        assert flag in out, f"help must list {flag}"
    # No UNINSTALL_TIMEOUT_S (P2-2 fix removed it)
    assert "UNINSTALL_TIMEOUT_S" not in out, (
        "UNINSTALL_TIMEOUT_S must not appear in help (P2-2 fix)"
    )


def test_bash32_no_bash4_syntax(tmp_path):
    """macOS /bin/bash is 3.2: the script must parse and run under bash 3.2,
    so no bash-4+ constructs — associative arrays, global declare,
    mapfile/readarray, or &> redirection."""
    script = REPO / "scripts" / "uninstall-local.sh"
    text = script.read_text(encoding="utf-8")
    import re as _re
    hits = []
    if _re.search(r"declare\s+-[Ag]", text):
        hits.append("declare -A/-g (bash 4.0+ only)")
    if _re.search(r"\b(mapfile|readarray)\b", text):
        hits.append("mapfile/readarray (bash 4.0+ only)")
    if _re.search(r"&(?!&)\s*>|&\s*>", text):
        hits.append("&> redirection (bash 4.0+ only; macOS bash 3.2 lacks it)")
    assert not hits, (
        "bash-4-only syntax in scripts/uninstall-local.sh: " + "; ".join(hits)
    )
