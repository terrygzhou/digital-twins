"""New-machine setup wizard (digital-twins setup).

Collapses the README's Step 2–5 into one command:

    digital-twins setup

The wizard:
  1. Detects the backend mode (already-configured / local Docker / cloud).
  2. For local Docker: runs the bootstrap logic inline (pull / build / up /
     health-poll) via subprocess — the same behaviour as
     scripts/bootstrap-local.sh but without requiring a git checkout.
  3. For cloud: prompts for the three required endpoints and writes
     kb.local.yml.
  4. Creates the state DB + migrations (the init step, minus the endpoint
     prompts, which are already in kb.local.yml at this point).
  5. Creates the first admin account with a generated password written to
     <state_dir>/admin-credentials.txt (chmod 600) and shown once.
  6. Runs the health checks (validate step) and prints the remediation for
     any failing endpoint.
  7. Prints the next-step hint (first ingest).

The function is importable for testing: each phase is a small callable,
and the top-level run_setup() composes them.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional
import urllib.error
import urllib.request

import click
import yaml

from digital_twins.config import load
from digital_twins.config.loader import local_config_path
from digital_twins.health import run_health_checks
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

# Local-stack endpoints written to kb.local.yml (mirror of the
# bootstrap-local.sh constants — keep in sync with docker-compose.yml).
_QDRANT_EP = "http://localhost:6333"
_NEO4J_EP = "bolt://localhost:7687"
_NEO4J_HTTP = "http://localhost:7474"
_LLM_EP = "http://localhost:8000/v1"
_EMBED_EP = "http://localhost:8080/v1"

_HEALTH_URLS = {
    "qdrant": "http://localhost:6333/healthz",
    "neo4j": "http://localhost:7687/",
    "llm": "http://localhost:8000/v1/models",
    "embedding-model": "http://localhost:8080/v1/models",
}

_HEALTH_TIMEOUT_S = 300  # per-service poll cap (5 min; the compose
# healthchecks do the real gating, this is just a host-side sanity poll)

_POLLEVERY_S = 2


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def docker_available() -> bool:
    """True when the docker CLI is present AND the daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def has_valid_local_config() -> bool:
    """True when kb.local.yml exists and carries a non-empty qdrant.url."""
    path = local_config_path()
    if not path.is_file():
        return False
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return bool((data.get("qdrant") or {}).get("url"))


def _kb_local_content(local: bool) -> dict:
    """The kb.local.yml payload for the local stack (all 4 endpoints)."""
    return {
        "qdrant": {"url": _QDRANT_EP},
        "neo4j": {"url": _NEO4J_EP},
        "llm": {"endpoint": _LLM_EP},
        "embedding": {"endpoint": _EMBED_EP},
    }


def _cloud_content(qdrant: str, neo4j: str, llm: str,
                   embedding: str = "",
                   neo4j_user: str = "",
                   neo4j_password: str = "") -> dict:
    data: dict = {"qdrant": {"url": qdrant}}
    neo: dict = {"url": neo4j}
    if neo4j_user:
        neo["user"] = neo4j_user
    if neo4j_password:
        neo["password"] = neo4j_password
    data["neo4j"] = neo
    data["llm"] = {"endpoint": llm}
    if embedding:
        data["embedding"] = {"endpoint": embedding}
    return data


def write_kb_local(data: dict) -> Path:
    """Write kb.local.yml (only when absent; leave an existing file alone)."""
    path = local_config_path()
    if path.is_file():
        click.echo(f"kb.local.yml already exists at {path} — leaving it untouched.")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    click.echo(f"wrote {path}")
    return path


# ---------------------------------------------------------------------------
# Local-stack bootstrap (in-Python port of scripts/bootstrap-local.sh)
# ---------------------------------------------------------------------------

def _docker_compose(args: list, compose_file: str = "docker-compose.yml") -> int:
    """Run `docker compose -f <file> <args>`; return the exit code."""
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode


def _poll_url(url: str, timeout_s: int = _HEALTH_TIMEOUT_S) -> bool:
    """Poll url until it returns HTTP 200 or the deadline passes."""
    deadline = time.time() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(_POLLEVERY_S)


def run_local_stack(prompt: Callable = click.confirm,
                    echo: Callable = click.echo) -> bool:
    """Bring up the bundled local stack and write kb.local.yml.

    Returns True when the stack is up (or when it was already up and the
    config file matched), False on any hard failure.
    """
    # Pull + build only when something is not already running.
    _docker_compose(["pull", "qdrant", "neo4j"])
    _docker_compose(["build", "digital-twins"])
    up_services = ["qdrant", "neo4j", "embedding-model", "digital-twins"]
    # GPU: probe nvidia-smi; when absent the bundled llm is intentionally
    # skipped (the no-GPU path documented in the README).
    gpu = shutil.which("nvidia-smi") is not None
    if gpu:
        up_services.append("llm")
    echo(f"starting local stack: {', '.join(up_services)}")
    rc = _docker_compose(["up", "-d", *up_services])
    if rc != 0:
        echo("docker compose up failed — run 'docker compose logs' to inspect.")
        return False

    # Health-poll the mandatory services (llm is non-fatal when skipped).
    failed = []
    for svc in ("qdrant", "neo4j"):
        if not _poll_url(_HEALTH_URLS[svc]):
            failed.append(svc)
    if gpu:
        if not _poll_url(_HEALTH_URLS["llm"]):
            echo("llm did not become healthy — point KB_LLM__ENDPOINT at an "
                 "external LLM; the rest of the stack is kept.")
    embed_ok = _poll_url(_HEALTH_URLS["embedding-model"])
    if not embed_ok:
        echo("embedding-model: UNHEALTHY — embedding falls back to the "
             "in-process default; the rest of the stack is kept.")
    if failed:
        echo(f"ERROR: service(s) did not become healthy: {', '.join(failed)}")
        echo("remediation: run 'docker compose logs <service>', then re-run setup.")
        return False

    content = _kb_local_content(local=True)
    # On no-GPU hosts the bundled llm/embedding are not part of the stack:
    # drop them so kb.local.yml doesn't point at ports that were never up.
    if not gpu:
        content.pop("llm", None)
        content.pop("embedding", None)
        echo("llm: SKIPPED (no suitable GPU — set KB_LLM__ENDPOINT to an "
             "external LLM to enable chat)")
    write_kb_local(content)
    echo("local stack is up: " +
         ", ".join(f"{s}: up" for s in up_services if s not in failed))
    return True


# ---------------------------------------------------------------------------
# Cloud mode
# ---------------------------------------------------------------------------

def run_cloud_stack(prompt_text: Callable = click.prompt,
                    echo: Callable = click.echo) -> bool:
    """Prompt for cloud endpoints and write kb.local.yml."""
    qdrant = os.environ.get("KB_QDRANT__URL") or prompt_text(
        "Cloud Qdrant URL (e.g. https://host:6333)")
    neo4j = os.environ.get("KB_NEO4J__URL") or prompt_text(
        "Cloud Neo4j URL (bolt:// or https://)")
    llm = os.environ.get("KB_LLM__ENDPOINT") or prompt_text(
        "Cloud LLM endpoint (OpenAI-compatible base URL, e.g. https://host/v1)")
    embedding = os.environ.get("KB_EMBEDDING__ENDPOINT") or ""
    neo4j_user = os.environ.get("KB_NEO4J__USER") or ""
    neo4j_password = os.environ.get("KB_NEO4J__PASSWORD") or ""
    if not (qdrant and neo4j and llm):
        echo("ERROR: cloud endpoints must be non-empty "
             "(set KB_QDRANT__URL / KB_NEO4J__URL / KB_LLM__ENDPOINT "
             "or answer the prompts).")
        return False
    write_kb_local(_cloud_content(qdrant, neo4j, llm,
                                  embedding, neo4j_user, neo4j_password))
    echo("cloud mode: no Docker required — kb.local.yml now points at the "
         "cloud endpoints.")
    return True


# ---------------------------------------------------------------------------
# Admin account
# ---------------------------------------------------------------------------

ADMIN_EMAIL_DEFAULT = "admin@localhost"


def create_admin_account(state_dir: Path,
                         email: Optional[str] = None) -> tuple:
    """Create the first admin account with a generated password.

    Returns (email, password). Idempotent: when the accounts table is
    non-empty, reads the first row back instead of creating a second admin.
    """
    email = os.environ.get("INIT_ADMIN_EMAIL") or email or ADMIN_EMAIL_DEFAULT
    conn = connect(state_dir)
    try:
        first = conn.execute(
            "SELECT email FROM accounts ORDER BY rowid LIMIT 1").fetchone()
        if first is not None:
            return first[0], ""  # already exists; no new password generated
        password = secrets.token_urlsafe(16)
        from digital_twins.accounts import create_account
        create_account(conn, email, password)
        # Persist the generated password so the user can find it later
        # (chmod 600 — same directory as state.db).
        cred_path = state_dir / "admin-credentials.txt"
        cred_path.write_text(
            f"email: {email}\npassword: {password}\n"
            f"# written by 'digital-twins setup' — delete after first login.\n",
            encoding="utf-8")
        cred_path.chmod(0o600)
        return email, password
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Top-level wizard
# ---------------------------------------------------------------------------

def run_setup(prompt: Callable = click.prompt,
              confirm: Callable = click.confirm,
              echo: Callable = click.echo,
              force_cloud: bool = False,
              skip_services: bool = False) -> int:
    """Run the full setup wizard. Returns the process exit code.

    force_cloud: skip docker detection, go straight to the cloud path.
    skip_services: assume the backend is already up; only do init + admin
    + validate (the "re-run" fast path).
    """
    # --- 1) backend mode ---------------------------------------------------
    if force_cloud:
        if not run_cloud_stack(prompt, echo):
            return 5
    elif has_valid_local_config():
        echo("kb.local.yml already has valid endpoints — skipping service "
             "startup (re-run 'digital-twins setup --cloud' to force cloud).")
    elif skip_services:
        pass
    elif docker_available() and confirm(
            "Docker is available. Start the bundled local stack? "
            "(qdrant + neo4j + embedding-model, ~1-2 min on first run)"):
        if not run_local_stack(prompt, echo):
            return 3
    else:
        # No docker, or the user declined the local stack: fall back to
        # cloud mode.
        echo("falling back to cloud mode.")
        if not run_cloud_stack(prompt, echo):
            return 5

    # --- 2) init (state DB + migrations, no endpoint prompts) --------------
    cfg = load()
    state_dir = Path(cfg["state_dir"]).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(state_dir)
    try:
        migrate(conn)
    finally:
        conn.close()

    # --- 3) first admin account --------------------------------------------
    admin_email, admin_pw = create_admin_account(state_dir)
    if admin_pw:
        echo(f"created first admin account: {admin_email}")
        echo(f"  password (shown once, also written to "
             f"{state_dir / 'admin-credentials.txt'}): {admin_pw}")
        echo("  delete the credentials file after your first login.")
    else:
        echo(f"admin already exists: {admin_email}")

    # --- 4) validate --------------------------------------------------------
    results = run_health_checks(load())
    for r in results:
        line = (f"  {r.endpoint:<9} {'ok' if r.ok else 'FAIL'}"
                f" {(r.status or '-'):<13} {r.detail}")
        if not r.ok:
            line += f"  -> {r.remediation}"
        echo(line)
    exit_code = 0 if all(r.ok for r in results) else 1

    # --- 5) next steps ------------------------------------------------------
    echo("")
    echo("Next: enable a source in kb.local.yml, then run:")
    echo("  digital-twins run --source fs")
    return exit_code
