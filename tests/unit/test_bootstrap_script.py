"""T031 (008 US3, RED): mocked-exec branch tests for scripts/bootstrap-local.sh.

Binding ruling R1: the script honours env ``BOOTSTRAP_EXEC`` — a prefix
prepended to every external command invocation via the single ``run_cmd()``
wrapper.  These tests set ``BOOTSTRAP_EXEC`` to a Python fake that:

* appends its argv to a JSONL log file (path via ``BOOTSTRAP_EXEC_LOG``), and
* returns canned stdout / exit codes keyed on the first argv element.

Canned behaviour is driven by a small JSON config the test writes per branch:
``FAKE_EXEC_CONFIG`` points at a JSON file with keys::

    {"docker": <exit_code_or_"error">,
     "nvidia-smi": {"exit": 0, "stdout": "..."},
     "curl": {"exit": 0, "stdout": "..."},
     ...}

Keyed on the *first* argv element (the command name).  For ``docker`` the
config may also carry a ``docker_args`` dict mapping an argv-prefix to a
specific canned response (e.g. ``{"pull": {"exit":0,"stdout":""}}``).

Each branch test asserts on:
* the exec log (JSONL: list of argv lists)
* the exit code of the script subprocess
* emitted lines on stdout/stderr (remediation messages, SKIPPED, etc.)
* the contents of ``kb.local.yml`` (written to a tmp dir via ``KB_CONFIG_DIR``)

The script is the T030–T032 RED stub — every branch test is expected to FAIL
because the stub prints "not implemented yet" and exits 1.  T033 replaces the
body; the ``run_cmd()`` contract stays.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "bootstrap-local.sh"

# ---------------------------------------------------------------------------
# Fake exec (BOOTSTRAP_EXEC) — written to a tmp dir by the fixture
# ---------------------------------------------------------------------------

FAKE_EXEC_PY = """\
#!/usr/bin/env python3
\"\"\"BOOTSTRAP_EXEC fake: log argv, return canned stdout/exit from config.\"\"\"
import json, os, sys

log_path = os.environ.get("BOOTSTRAP_EXEC_LOG")
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
    # For curl, allow keying on a substring of the URL (argv[2]) so a single
    # healthcheck endpoint can be unhealthy while others stay healthy (R3).
    if cmd == "curl":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        for substring, sub in entry.get("curl_args", {}).items():
            if substring in url:
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
def kb_config_dir(tmp_path: Path) -> Path:
    """KB_CONFIG_DIR target — kb.local.yml lands here."""
    d = tmp_path / "kb"
    d.mkdir()
    return d


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
    kb_config_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the bootstrap script with the fake exec injected."""
    env = {
        **os.environ,
        "BOOTSTRAP_EXEC": str(fake_exec),
        "BOOTSTRAP_EXEC_LOG": str(exec_log),
        "FAKE_EXEC_CONFIG": str(fake_config),
        "KB_CONFIG_DIR": str(kb_config_dir),
        "BOOTSTRAP_TIMEOUT_S": "2",
    }
    if extra_env:
        env.update(extra_env)
    # Remove KB_CONFIG_DIR from real env if present
    env.pop("KB_CONFIG_DIR", None)
    env["KB_CONFIG_DIR"] = str(kb_config_dir)
    return subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _kb_local(kb_config_dir: Path) -> Path:
    return kb_config_dir / "kb.local.yml"


# ---------------------------------------------------------------------------
# Branch tests — all expected to FAIL against the RED stub
# ---------------------------------------------------------------------------


def test_docker_missing_exits_1_with_remediation(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 1: docker not found → exit 1 + remediation, no kb.local.yml."""
    # docker command not in config → fake returns exit 1 (simulating not found)
    _write_config(fake_config, {"docker": 127})

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    # Remediation message on stderr
    combined = (result.stdout + result.stderr).lower()
    assert "docker" in combined, (
        f"expected remediation mentioning docker; got: {result.stderr!r}"
    )
    # No config write
    assert not _kb_local(kb_config_dir).exists(), (
        "kb.local.yml should NOT be written when docker is missing"
    )
    # Exec log: no docker compose pull called
    entries = _exec_log_entries(exec_log)
    pull_calls = [e for e in entries if len(e) > 2 and e[0] == "docker" and "pull" in e[1:3]]
    assert not pull_calls, f"docker compose pull should not be called: {pull_calls}"


def test_daemon_down_exits_1_with_remediation(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 2: daemon down → exit 1 + remediation (same family as #1)."""
    # docker exists but daemon check fails
    _write_config(
        fake_config,
        {
            "docker": {
                "exit": 1,
                "stderr": "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
                "docker_args": {},
            }
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "docker" in combined and ("daemon" in combined or "remediat" in combined or "start" in combined), (
        f"expected daemon remediation; got stderr={result.stderr!r}"
    )
    assert not _kb_local(kb_config_dir).exists(), (
        "kb.local.yml should NOT be written when daemon is down"
    )


def test_port_6333_in_use_exits_2(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 3: port 6333 in use → exit 2 + conflicting service named, no config write."""
    # docker works, but port check finds 6333 taken
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "qdrant-already-running"},
            "ss": {"exit": 0, "stdout": "LISTEN  0  128  0.0.0.0:6333  0.0.0.0:*  users:((\"some_other_service\",pid=1234))"},
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "6333" in combined, (
        f"expected port 6333 in output; got: {combined!r}"
    )
    assert not _kb_local(kb_config_dir).exists(), (
        "kb.local.yml should NOT be written on port conflict"
    )


def test_no_gpu_llm_skipped_exit_0(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 4: no-GPU (nvidia-smi absent) → llm skipped, exit 0,
    kb.local.yml written WITHOUT embedding.endpoint, SKIPPED line with
    external-LLM guidance (KB_LLM__ENDPOINT).
    """
    # nvidia-smi not found (exit 127)
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "nvidia-smi": 127,
            "curl": {"exit": 0, "stdout": "healthy"},
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "skipped" in combined.lower(), (
        f"expected SKIPPED line for llm; got: {combined!r}"
    )
    assert "kb_llm__endpoint" in combined or "external" in combined.lower(), (
        f"expected external-LLM guidance; got: {combined!r}"
    )
    # kb.local.yml written
    kb = _kb_local(kb_config_dir)
    assert kb.exists(), "kb.local.yml should be written on no-GPU success"
    content = kb.read_text(encoding="utf-8")
    # No embedding.endpoint key
    assert "embedding:" not in content or "endpoint" not in content, (
        f"kb.local.yml should NOT contain embedding.endpoint; got: {content!r}"
    )
    # No llm endpoint (llm was skipped)
    assert "llm:" not in content or "endpoint" not in content, (
        f"kb.local.yml should NOT contain llm endpoint when llm skipped; got: {content!r}"
    )


def test_gpu_present_full_stack_exit_0(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 5: GPU present → full stack, exit 0, kb.local.yml with local endpoints."""
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "nvidia-smi": {
                "exit": 0,
                "stdout": "GPU 0: NVIDIA A100\n",
            },
            "curl": {"exit": 0, "stdout": "healthy"},
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    kb = _kb_local(kb_config_dir)
    assert kb.exists(), "kb.local.yml should be written on full-stack success"
    content = kb.read_text(encoding="utf-8")
    # Should have local endpoints for qdrant, neo4j, llm
    assert "qdrant" in content, f"expected qdrant in kb.local.yml; got: {content!r}"
    assert "neo4j" in content, f"expected neo4j in kb.local.yml; got: {content!r}"
    assert "llm" in content, f"expected llm in kb.local.yml; got: {content!r}"
    # The written URLs must be the service endpoints (base URLs the package
    # clients consume), NOT the health-probe URLs. QdrantClient appends REST
    # paths, build_llm_client appends /chat/completions, the Neo4j driver
    # uses bolt://.  A /healthz or /v1/models suffix would 404.
    assert "url: http://localhost:6333" in content, (
        f"qdrant.url must be the base endpoint (no /healthz suffix); got: {content!r}"
    )
    assert "url: bolt://localhost:7687" in content, (
        f"neo4j.url must be bolt:// (not http://); got: {content!r}"
    )
    assert "endpoint: http://localhost:8000/v1" in content, (
        f"llm.endpoint must be the /v1 base (no /models suffix); got: {content!r}"
    )


def test_health_timeout_exits_3(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 6: health timeout (non-llm service never healthy) → exit 3,
    services named, docker compose logs hint, no config write.
    """
    # curl for health checks returns unhealthy
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "nvidia-smi": 127,
            "curl": {"exit": 1, "stdout": "Service Unavailable"},
        },
    )

    result = _run_script(
        fake_exec, exec_log, fake_config, kb_config_dir,
        extra_env={"BOOTSTRAP_TIMEOUT_S": "1"},
    )

    assert result.returncode == 3, (
        f"expected exit 3, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "docker compose logs" in combined or "logs" in combined, (
        f"expected docker compose logs hint; got: {combined!r}"
    )
    assert not _kb_local(kb_config_dir).exists(), (
        "kb.local.yml should NOT be written on health timeout"
    )


def test_healthy_rerun_zero_pulls_zero_writes(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 7: healthy re-run → 0 pulls, 0 builds, 0 state writes,
    per-service status printed, exit 0.
    """
    # Pre-write a matching kb.local.yml
    kb = _kb_local(kb_config_dir)
    kb.write_text(
        "qdrant:\n  url: http://localhost:6333\n"
        "neo4j:\n  url: bolt://localhost:7687\n"
        "llm:\n  endpoint: http://localhost:8000/v1\n",
        encoding="utf-8",
    )

    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "qdrant: running\nneo4j: running\nllm: running\ndigital-twins: running\n"},
            "nvidia-smi": {"exit": 0, "stdout": "GPU 0: NVIDIA A100\n"},
            "curl": {"exit": 0, "stdout": "healthy"},
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    # No docker compose pull
    pull_calls = [e for e in entries if "pull" in " ".join(e)]
    assert not pull_calls, f"no pulls expected on healthy re-run: {pull_calls}"
    # No docker compose build
    build_calls = [e for e in entries if "build" in " ".join(e)]
    assert not build_calls, f"no builds expected on healthy re-run: {build_calls}"
    # Per-service status
    combined = result.stdout + result.stderr
    for svc in ("qdrant", "neo4j", "digital-twins"):
        assert svc in combined, f"expected {svc} in status output; got: {combined!r}"


def test_nogpu_healthy_rerun_zero_pulls_zero_writes(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 7b: healthy re-run on a NO-GPU host.  The bundled llm service
    is intentionally never started there, so its absent "running" state must
    not trigger pulls or builds (FR-006 / SC-004).
    """
    # Pre-write a no-GPU kb.local.yml (no llm endpoint; in-process embedder).
    kb = _kb_local(kb_config_dir)
    kb.write_text(
        "qdrant:\n  url: http://localhost:6333\n"
        "neo4j:\n  url: bolt://localhost:7687\n",
        encoding="utf-8",
    )

    _write_config(
        fake_config,
        {
            # No llm line: the service is not started on this host.
            "docker": {"exit": 0, "stdout": "qdrant: running\nneo4j: running\ndigital-twins: running\n"},
            "nvidia-smi": 127,
            "curl": {"exit": 0, "stdout": "healthy"},
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    entries = _exec_log_entries(exec_log)
    pull_calls = [e for e in entries if "pull" in " ".join(e)]
    assert not pull_calls, (
        f"no pulls expected on healthy no-GPU re-run (llm intentionally "
        f"skipped): {pull_calls}"
    )
    build_calls = [e for e in entries if "build" in " ".join(e)]
    assert not build_calls, f"no builds expected: {build_calls}"


def test_kb_local_yml_disagreeing_endpoints_untouched(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 8: kb.local.yml exists with disagreeing endpoints → diff + warning,
    file byte-identical after run, exit 0.
    """
    kb = _kb_local(kb_config_dir)
    original_content = (
        "qdrant:\n  url: http://some-other-host:6333\n"
        "neo4j:\n  url: bolt://other:7687\n"
    )
    kb.write_text(original_content, encoding="utf-8")

    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "nvidia-smi": 127,
            "curl": {"exit": 0, "stdout": "healthy"},
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "warn" in combined.lower() or "diff" in combined.lower(), (
        f"expected warning/diff for disagreeing endpoints; got: {combined!r}"
    )
    # File byte-identical
    assert kb.read_text(encoding="utf-8") == original_content, (
        "kb.local.yml should be byte-identical (untouched)"
    )


def test_embedding_model_healthcheck_fail_exit_0(
    fake_exec, exec_log, fake_config, kb_config_dir
):
    """Branch 9 (R3 extra): embedding-model healthcheck never passes →
    reported explicitly, rest of stack up, exit 0, kb.local.yml with
    qdrant/neo4j endpoints, embedding.endpoint UNSET.
    """
    # curl: embedding-model endpoint (port 8080) is unhealthy, others healthy.
    # The fake exec now keys on the curl URL via "curl_args" (a substring of the
    # URL), mirroring the docker_args prefix mechanism — so port 8080 returns
    # unhealthy while qdrant/neo4j/llm stay healthy. This genuinely models the
    # embedding-model healthcheck defect (R3) instead of treating all URLs as healthy.
    _write_config(
        fake_config,
        {
            "docker": {"exit": 0, "stdout": "", "docker_args": {}},
            "nvidia-smi": 127,
            "curl": {
                "exit": 0,
                "stdout": "healthy",
                "curl_args": {
                    "8080": {"exit": 1, "stdout": "Service Unavailable"},
                },
            },
        },
    )

    result = _run_script(fake_exec, exec_log, fake_config, kb_config_dir)

    assert result.returncode == 0, (
        f"expected exit 0 (embedding-model failure is non-fatal), got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "embedding" in combined.lower(), (
        f"expected explicit embedding-model report; got: {combined!r}"
    )
    kb = _kb_local(kb_config_dir)
    assert kb.exists(), "kb.local.yml should be written"
    content = kb.read_text(encoding="utf-8")
    assert "qdrant" in content, f"expected qdrant in kb.local.yml; got: {content!r}"
    assert "neo4j" in content, f"expected neo4j in kb.local.yml; got: {content!r}"
    # embedding.endpoint must be UNSET
    assert "embedding" not in content, (
        f"embedding.endpoint should be UNSET in kb.local.yml; got: {content!r}"
    )


def test_bootstrap_script_no_bash4_only_syntax(tmp_path):
    """macOS /bin/bash is 3.2 (the `declare: [-afFirtx]` usage string is its
    fingerprint): the script must parse and run under bash 3.2, so no
    bash-4+ constructs — associative arrays (`declare -A`), global
    `declare -g`, `mapfile`/`readarray`, or the `&>` redirection.
    A 2026-09-08 macOS run died at `declare -A PORT_TO_SERVICE` (line 49).
    """
    script = REPO / "scripts" / "bootstrap-local.sh"
    text = script.read_text(encoding="utf-8")
    import re as _re
    hits = []
    if _re.search(r"declare\s+-[Ag]", text):
        hits.append("declare -A/-g (bash 4.0+ only)")
    if _re.search(r"\b(mapfile|readarray)\b", text):
        hits.append("mapfile/readarray (bash 4.0+ only)")
    if _re.search(r"&(?!&)\s*>|&\s*>", text):
        hits.append("&> redirection (bash 4.0+ only; macOS bash 3.2 lacks it)")
    assert not hits, "bash-4-only syntax in scripts/bootstrap-local.sh: " + "; ".join(hits)


def test_help_lists_docker_prerequisites():
    """--help must state the Docker precondition up front (owner feedback:
    'if docker is mandatory, it should be part of the conditions of
    running bootstrap'). The exit-1 fail-fast only tells the user AFTER
    they run it; the help text must name the requirements BEFORE.
    """
    result = subprocess.run(
        [str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "BOOTSTRAP_EXEC": ""},  # help path runs no externals
    )
    assert result.returncode == 0, f"--help exit {result.returncode}: {result.stderr}"
    out = result.stdout
    assert "Prerequisites" in out, "help lacks a Prerequisites section"
    # The three contract exit-1 conditions, named up front:
    assert "daemon" in out.lower(), "help does not name the running-daemon requirement"
    assert "compose" in out.lower(), "help does not name the compose requirement"
    assert "exit 1" in out, "help does not state the missing-Docker exit code"
    # No-GPU hosts remain supported (LLM skipped), so the precondition list
    # must not claim a GPU is required:
    gpu_lines = [ln for ln in out.splitlines() if "GPU" in ln]
    assert any("optional" in ln.lower() for ln in gpu_lines), (
        "help must state GPU is optional (no-GPU host: bundled llm skipped)"
    )
