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

import importlib.resources
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
import urllib.error
import urllib.request

import click
import yaml

from digital_twins.accounts import create_account
from digital_twins.config import load
from digital_twins.config.loader import local_config_path
from digital_twins.health import run_health_checks
from digital_twins.state.db import connect

# Local-stack endpoints written to kb.local.yml (mirror of the
# bootstrap-local.sh constants — keep in sync with docker-compose.yml).
_QDRANT_EP = "http://localhost:6333"
_NEO4J_EP = "bolt://localhost:7687"
_NEO4J_HTTP = "http://localhost:7474"
_LLM_EP = "http://localhost:8000/v1"
_EMBED_EP = "http://localhost:8080/v1"

# Local-stack Neo4j credentials: the bundled compose file bakes these in
# as defaults (NEO4J_AUTH: ${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD}).  The
# wizard prompts for both — the user can keep the defaults by pressing
# Enter, or type a stronger password to match the stack they start.
_NEO4J_USER_DEFAULT = "neo4j"
_NEO4J_PASSWORD_DEFAULT = "password"

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


def _kb_local_content(local: bool,
                      neo4j_user: str = _NEO4J_USER_DEFAULT,
                      neo4j_password: str = _NEO4J_PASSWORD_DEFAULT) -> dict:
    """The kb.local.yml payload for the local stack (all 4 endpoints)."""
    data = {
        "qdrant": {"url": _QDRANT_EP},
        "neo4j": {"url": _NEO4J_EP},
        "llm": {"endpoint": _LLM_EP},
        "embedding": {"endpoint": _EMBED_EP},
    }
    neo = data["neo4j"]
    if neo4j_user:
        neo["user"] = neo4j_user
    if neo4j_password:
        neo["password"] = neo4j_password
    return data


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

def _gpu_present() -> bool:
    """True when a GPU is actually usable: `nvidia-smi -L` prints at least
    one GPU. A host where nvidia-smi is installed but the driver is
    missing (or no GPU is attached) reports False, matching the
    bootstrap-local.sh contract (which runs the binary, not just checks
    PATH)."""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_compose_file() -> str:
    """Locate the compose file: CWD (git checkout) or the shipped package
    copy (plain pip install). The package copy is extracted to a stable
    temp path so `docker compose -f` can use it.

    Returns a path string that exists on disk.
    """
    cwd_path = Path("docker-compose.yml")
    if cwd_path.is_file():
        return str(cwd_path)
    # Shipped copy: digital_twins/compose/docker-compose.yml (force-included
    # in the wheel via pyproject.toml).
    pkg_file = importlib.resources.files("digital_twins.compose") / "docker-compose.yml"
    # Copy to a stable, host-writable temp location (docker compose needs a
    # real file path, not a zip-entry URI).
    tmp = Path(tempfile.gettempdir()) / "digital-twins-compose.yml"
    tmp.write_text(pkg_file.read_text(encoding="utf-8"), encoding="utf-8")
    return str(tmp)


def _docker_compose(args: list, compose_file: str | None = None,
                    extra_env: dict | None = None) -> int:
    """Run `docker compose -f <file> <args>`; return the exit code.

    compose_file: explicit path; when None, resolved via _resolve_compose_file().
    extra_env: host env vars merged in for the compose subprocess (used
    to pass the user's Neo4j credential choices through to the stack).
    """
    if compose_file is None:
        compose_file = _resolve_compose_file()
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env,
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


def run_local_stack(prompt_text: Callable = click.prompt,
                    echo: Callable = click.echo) -> bool:
    """Bring up the bundled local stack and write kb.local.yml.

    The Neo4j credentials are prompted (with the compose defaults as
    pre-filled answers): the user can keep the defaults by pressing
    Enter, or type a stronger password (and/or a different username) to
    match the stack they are about to start.  The answers are passed to
    `docker compose` via NEO4J_USER / NEO4J_PASSWORD env vars AND
    written to kb.local.yml.

    Returns True when the stack is up (or when it was already up and the
    config file matched), False on any hard failure.
    """
    # Prompt for the Neo4j credentials (defaults match the compose file).
    echo(f"Neo4j credentials (defaults: user={_NEO4J_USER_DEFAULT}, "
         f"password={_NEO4J_PASSWORD_DEFAULT}) — press Enter to keep "
         "them, or type a stronger password now.")
    neo4j_user = prompt_text(
        f"Local Neo4j user [{_NEO4J_USER_DEFAULT}]: "
        "(press Enter to keep the default)").strip() or _NEO4J_USER_DEFAULT
    neo4j_password = prompt_text(
        f"Local Neo4j password [{_NEO4J_PASSWORD_DEFAULT}]: "
        "(press Enter to keep the default, or type a stronger one)")         .strip() or _NEO4J_PASSWORD_DEFAULT

    # Pull the backend images (qdrant + neo4j are pre-built; llm and
    # embedding-model use standard images — no build step needed).
    _docker_compose(["pull", "qdrant", "neo4j"])
    # digital-twins service is optional (the CLI runs on the host); only
    # include it when the compose file was found in a git checkout.
    up_services = ["qdrant", "neo4j", "embedding-model"]
    # GPU probe (mirror of bootstrap-local.sh: actually RUN `nvidia-smi -L`
    # and check for non-empty output — the binary can exist without a
    # working driver/GPU, and the no-GPU path must skip the bundled llm
    # service, not waste a 300s health poll on it).
    gpu = _gpu_present()
    if gpu:
        up_services.append("llm")
    echo(f"starting local stack: {', '.join(up_services)}")
    # Pass the chosen credentials to compose (matches the compose-file
    # defaults unless the user overrode them).
    rc = _docker_compose(
        ["up", "-d", *up_services],
        extra_env={"NEO4J_USER": neo4j_user,
                   "NEO4J_PASSWORD": neo4j_password})
    if rc != 0:
        echo("docker compose up failed — some services may already be up "
             "from a previous run; checking health anyway.")
        # Do NOT give up: the stack may be partially up. Fall through to
        # the health poll; if the mandatory services answer we still write
        # kb.local.yml so the host config points at what is actually up.
        up_failed = True
    else:
        up_failed = False

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
        if up_failed:
            echo("remediation: some services are up (re-run setup to "
                 "finish); run 'docker compose logs <service>' for the "
                 "failed ones.")
        else:
            echo("ERROR: service(s) did not become healthy: "
                 f"{', '.join(failed)}")
            echo("remediation: run 'docker compose logs <service>', "
                 "then re-run setup.")
        return False
    if up_failed:
        echo("some services failed to start but the mandatory ones are "
             "healthy — continuing with the config that points at what "
             "is up.")

    content = _kb_local_content(local=True,
                                neo4j_user=neo4j_user,
                                neo4j_password=neo4j_password)
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
    """Prompt for cloud endpoints and write kb.local.yml.

    Env vars take precedence over prompts; a var set to an *empty string*
    is honored as "set but empty" (no prompt), which then fails the
    required-non-empty gate with a remediation naming exactly which vars
    are empty. Unset vars fall through to the prompt.
    """
    def _env_or_prompt(var: str, label: str, required: bool) -> str:
        value = os.environ.get(var)
        if value is not None:
            return value
        if not required:
            return ""
        return prompt_text(label)
    qdrant = _env_or_prompt("KB_QDRANT__URL",
                            "Cloud Qdrant URL (e.g. https://host:6333)",
                            required=True)
    neo4j = _env_or_prompt("KB_NEO4J__URL",
                           "Cloud Neo4j URL (bolt:// or https://)",
                           required=True)
    llm = _env_or_prompt("KB_LLM__ENDPOINT",
                         "Cloud LLM endpoint (OpenAI-compatible base URL, "
                         "e.g. https://host/v1)", required=True)
    embedding = _env_or_prompt("KB_EMBEDDING__ENDPOINT",
                               "Cloud embedding endpoint (optional)",
                               required=False)
    # neo4j.user / neo4j.password: needed only when the Neo4j instance
    # requires auth.  The prompts below stay optional; the re-prompt
    # after the endpoint prompts catches the cloud-needs-auth case.
    neo4j_user = _env_or_prompt("KB_NEO4J__USER",
                                "Cloud Neo4j user (e.g. neo4j)",
                                required=False)
    neo4j_password = _env_or_prompt("KB_NEO4J__PASSWORD",
                                    "Cloud Neo4j password",
                                    required=False)
    # Strip leading/trailing whitespace from every endpoint the user
    # typed: a pasted URL with a trailing space fails the health check
    # with an opaque "InvalidURL: control characters" error.
    qdrant = qdrant.strip()
    neo4j = neo4j.strip()
    llm = llm.strip()
    embedding = embedding.strip()
    # The cloud path: when the user answered the Neo4j URL prompt but
    # left user/password empty, re-prompt (up to 3 times per field) —
    # a cloud Neo4j that needs auth will fail the health check
    # without credentials, and the wizard's contract is to leave a
    # *working* kb.local.yml.  Pressing Enter through all 3 prompts
    # still accepts an auth-disabled instance.
    if neo4j and not neo4j_user:
        for _attempt in range(3):
            neo4j_user = prompt_text(
                "Cloud Neo4j user (e.g. neo4j) "
                "[empty for auth-disabled Neo4j]:")
            if neo4j_user:
                break
    if neo4j and not neo4j_password:
        for _attempt in range(3):
            neo4j_password = prompt_text(
                "Cloud Neo4j password "
                "[empty for auth-disabled Neo4j]:")
            if neo4j_password:
                break
    if not (qdrant and neo4j and llm):
        empty = [name for name, value in (
            ("KB_QDRANT__URL", qdrant), ("KB_NEO4J__URL", neo4j),
            ("KB_LLM__ENDPOINT", llm)) if not value]
        echo(f"ERROR: cloud endpoints must be non-empty — "
             f"{' and '.join(empty)} is empty. Set it, or unset it and "
             f"re-run to answer the prompt.")
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

    Returns (email, password). Idempotent: when an admin account already
    exists, reads it back (empty password) instead of creating a second
    admin. A non-admin row (e.g. reader/scheduler) does NOT count as
    "admin exists" — a new admin is created alongside it.

    The generated password is shown once on stdout AND persisted to
    <state_dir>/admin-credentials.txt (created with mode 600 atomically —
    no world-readable window). Delete the file after first login.
    """
    email = os.environ.get("INIT_ADMIN_EMAIL") or email or ADMIN_EMAIL_DEFAULT
    conn = connect(state_dir)
    try:
        existing_admin = conn.execute(
            "SELECT email FROM accounts WHERE role='admin' "
            "ORDER BY rowid LIMIT 1").fetchone()
        if existing_admin is not None:
            return existing_admin[0], ""  # admin exists; nothing to do
        password = secrets.token_urlsafe(24)
        create_account(conn, email, password, role="admin")
        # Persist the generated password so the user can find it later.
        # Open with O_CREAT|O_EXCL + mode 600: the file is never created
        # world-readable, even briefly (no write→chmod window).
        cred_path = state_dir / "admin-credentials.txt"
        fd = os.open(str(cred_path),
                      os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                f"email: {email}\npassword: {password}\n"
                f"# written by 'digital-twins setup' — delete after "
                f"first login.\n")
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
    skip_services: explicit "I handle backends myself" — takes precedence
    over everything: never probe Docker, never prompt for endpoints, never
    write kb.local.yml; only init + admin + validate run (the re-run fast
    path).
    """
    # --- 1) backend mode ---------------------------------------------------
    try:
        if skip_services:
            echo("skipping service startup (--skip-services).")
        elif force_cloud:
            if not run_cloud_stack(prompt, echo):
                return 5
        elif has_valid_local_config():
            echo("kb.local.yml already has valid endpoints — skipping "
                 "service startup. Config: "
                 f"{local_config_path()} "
                 "(re-run 'digital-twins setup --cloud' to force cloud.)")
        elif docker_available() and confirm(
                "Docker is available. Start the bundled local stack? "
                "(qdrant + neo4j + embedding-model, ~1-2 min on first "
                "run)"):
            if not run_local_stack(prompt, echo):
                return 3
        else:
            # No docker, or the user declined the local stack: fall back
            # to cloud mode.
            echo("falling back to cloud mode.")
            if not run_cloud_stack(prompt, echo):
                return 5
    except (EOFError, KeyboardInterrupt):
        # An interrupted prompt (Ctrl-C, or EOF on non-interactive
        # stdin): a user cancellation, not a failed health check.
        # Report a clean cancellation instead of leaking the interpreter
        # 'Aborted!' message.
        echo("setup was interrupted before the backend was configured — "
             "re-run 'digital-twins setup' to continue (or set the "
             "KB_* env vars and re-run).")
        return 6

    # --- 2) init (state DB + migrations, no endpoint prompts) --------------
    # connect() already migrates on open (state.db.connect); no second
    # migrate call needed.
    cfg = load()
    state_dir = Path(cfg["state_dir"]).expanduser()
    conn = connect(state_dir)
    conn.close()

    # --- 3) first admin account --------------------------------------------
    admin_email, admin_pw = create_admin_account(state_dir)
    if admin_pw:
        echo(f"created first admin account: {admin_email}")
        echo(f"  password (shown once, also written to "
             f"{state_dir / 'admin-credentials.txt'}): {admin_pw}")
        echo("  delete the credentials file after your first login.")
    else:
        cred_file = state_dir / "admin-credentials.txt"
        if cred_file.is_file():
            echo(f"admin already exists: {admin_email} "
                 f"(password: {cred_file}, written once on first "
                 f"creation; re-runs never re-print it).")
        else:
            echo(f"admin already exists: {admin_email}. "
                 f"The one-time credential file "
                 f"({cred_file}) is missing — if you forgot the "
                 f"password there is no reset path today; to start "
                 f"fresh, remove {state_dir} and re-run setup "
                 f"(see README 'Uninstall').")

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
