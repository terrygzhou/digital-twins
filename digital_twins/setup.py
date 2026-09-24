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

# The four backend services that the user may point at a *local* bundled
# Docker service or at an *external* endpoint, independently of each other
# (install-setup-separation D2).  Each maps to a bundled URL constant and to
# the env var that, in --cloud-env mode, supplies its external URL.
# `gpu_only` marks services whose bundled service only starts on a GPU host;
# `required` marks services whose external URL is mandatory (vs optional).
_SERVICE_ENV = {
    "qdrant": ("KB_QDRANT__URL", _QDRANT_EP, False, True),
    "neo4j": ("KB_NEO4J__URL", _NEO4J_EP, False, True),
    "llm": ("KB_LLM__ENDPOINT", _LLM_EP, True, True),
    "embedding": ("KB_EMBEDDING__ENDPOINT", _EMBED_EP, True, False),
}


def parse_backends(spec: str) -> dict:
    """Parse a ``--backends`` CLI value into a per-service choice map.

    ``spec`` is a comma-separated list of ``KEY=VALUE`` pairs, e.g.
    ``"qdrant=local,llm=https://example.com/v1"``.  Each VALUE is either
    ``"local"`` (the bundled Docker service), a URL (any non-"local"
    string), or ``""`` (empty — the service is required-external and the
    caller must supply the URL later; exit 5 in --cloud-env mode).

    Returns ``{service: value}`` (values keep the "local"/URL/"" distinction).
    Raises ``ValueError`` for an unknown service name (fail fast on typos).
    """
    out: dict = {}
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        key, sep, value = item.partition("=")
        if not sep:
            # A bare "qdrant" with no "=" — treat as "local"? No: be strict.
            raise ValueError(
                f"--backends entry {item!r} is missing '=' "
                "(expected KEY=VALUE, e.g. qdrant=local)")
        key = key.strip()
        if key not in _SERVICE_ENV:
            known = ", ".join(sorted(_SERVICE_ENV))
            raise ValueError(
                f"unknown service {key!r} in --backends "
                f"(known: {known})")
        out[key] = value.strip()
    return out


def resolve_backends(backends: dict | None = None,
                     *,
                     local: bool = False,
                     cloud: bool = False,
                     cloud_env: bool = False,
                     gpu: bool = False,
                     docker: bool = False,
                     env: dict | None = None,
                     neo4j_user: str = "",
                     neo4j_password: str = "") -> dict:
    """Resolve the per-service backend choice into a deterministic map.

    Returns ``{service: {"mode": "local"|"external", "url": str, ...}}`` for
    all four of ``qdrant/neo4j/llm/embedding``.  Pure function of its inputs —
    no I/O, no prompts, no docker — so it is trivially unit-testable.

    Inputs:
      backends: explicit ``--backends`` map of ``{service: "local"|URL}``.
                Services not listed default to *local* (the interactive
                default); an explicit external with an empty URL is a
                required-missing endpoint, not a local one.
      local / cloud / cloud_env: shorthand flags (``--local``, ``--cloud``,
                ``--cloud-env``); when none of the three is set the caller
                drives the choice interactively, which is expressed here by
                omitting all of them (every unlisted service -> local).
      gpu: whether a usable GPU is present (the bundled llm/embedding only
                start on a GPU host).
      env: mapping used to look up external URLs in --cloud-env mode;
                defaults to ``os.environ`` when None.
      neo4j_user / neo4j_password: carried through on the neo4j entry so the
                caller can pass them to ``docker compose`` / the health check.

    Per entry:
      mode=="local": url is the bundled URL constant; the caller only starts
        this service in Docker when its mode is local.
      mode=="external": url is the user/env value (may be ""); a required
        service with an empty url is flagged ``required=True`` (the caller
        prompts, or in --cloud-env fails the gate naming the env var).
      llm/embedding local on a no-GPU host: mode flips to external, url empty
        (or the env value in --cloud-env), and ``unavailable=True`` marks
        "local was requested but the bundled service cannot start here".
    """
    if env is None:
        env = os.environ
    explicit = {k: str(v).strip() for k, v in (backends or {}).items()}

    # Which services resolve to external from the shorthand flags.
    all_external = bool(cloud or cloud_env)
    if local:
        all_external = False  # --local forces local for any unlisted service

    out: dict = {}
    for svc, (env_name, local_url, gpu_only, required) in _SERVICE_ENV.items():
        entry: dict = {"mode": "external" if all_external else "local",
                       "url": "",
                       "required": False,
                       "env": env_name}
        choice = explicit.get(svc)
        if choice is not None:
            if choice == "local":
                entry["mode"] = "local"
                entry["url"] = local_url
            else:
                # explicit external: URL (possibly empty -> required-missing)
                entry["mode"] = "external"
                entry["url"] = choice
                if choice == "":
                    entry["required"] = required or svc in ("qdrant", "neo4j", "llm")
        # In --cloud-env mode, unlisted (default-external) services pull their
        # URL from the env var; a missing/empty var on a required service is
        # the gate that names the var.
        if cloud_env and choice is None:
            entry["mode"] = "external"
            entry["url"] = (env.get(env_name) or "").strip()
            entry["required"] = required
        elif entry["mode"] == "local":
            entry["url"] = local_url
        # llm/embedding local on a no-GPU host is unavailable: flip to
        # external and mark it so the caller surfaces an external URL.
        if svc in ("llm", "embedding") and not gpu and entry["mode"] == "local":
            entry["mode"] = "external"
            entry["url"] = (env.get(env_name) or "").strip()
            entry["unavailable"] = True
            if svc == "llm":
                entry["required"] = True
        if svc == "neo4j":
            if neo4j_user:
                entry["user"] = neo4j_user
            if neo4j_password:
                entry["password"] = neo4j_password
        out[svc] = entry
    return out



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
                    extra_env: dict | None = None,
                    output: list | None = None) -> int:
    """Run `docker compose -f <file> <args>`; return the exit code.

    compose_file: explicit path; when None, resolved via _resolve_compose_file().
    extra_env: host env vars merged in for the compose subprocess (used
    to pass the user's Neo4j credential choices through to the stack).
    output: optional list; when given, the tail of the combined
    stdout+stderr (last 40 non-empty lines) is appended to it so callers
    can surface the actual compose error on failure instead of a blank
    fallback message.
    """
    if compose_file is None:
        compose_file = _resolve_compose_file()
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, *args],
        capture_output=True, text=True,
        env=env,
    )
    if output is not None:
        text = (result.stdout or "") + (result.stderr or "")
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            output.append("\n".join(lines[-40:]))
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
                    echo: Callable = click.echo,
                    resolved: dict | None = None) -> bool:
    """Bring up the bundled local stack and write kb.local.yml.

    The Neo4j credentials are prompted (with the compose defaults as
    pre-filled answers): the user can keep the defaults by pressing
    Enter, or type a stronger password (and/or a different username) to
    match the stack they are about to start.  The answers are passed to
    `docker compose` via NEO4J_USER / NEO4J_PASSWORD env vars AND
    written to kb.local.yml.

    resolved: the per-service resolved map from resolve_backends()
    (install-setup-separation T1.3).  When provided, ``up_services``
    is built from the services whose mode is "local" (only those are
    started in Docker) and kb.local.yml is written from the resolved
    map so mixed local/external choices land in one file.  When None,
    the legacy path is unchanged (hardcoded up_services list, all-local
    content via _kb_local_content()).

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
    pull_out: list = []
    pull_rc = _docker_compose(["pull", "qdrant", "neo4j"], output=pull_out)
    if pull_rc != 0:
        echo("docker compose pull failed (images may be missing or the "
             "network is unreachable):")
        for line in (pull_out[0].splitlines() if pull_out else []):
            echo(f"  {line}")
        echo("continuing anyway — `up` will pull anything missing.")
    # digital-twins service is optional (the CLI runs on the host); only
    # include it when the compose file was found in a git checkout.
    if resolved is not None:
        # T1.3: the resolved map drives which services actually start in
        # Docker — only the services whose mode is "local" (the compose
        # names mirror the service keys; "embedding" is
        # "embedding-model" in docker-compose.yml).
        _COMPOSE_NAME = {"embedding": "embedding-model"}
        up_services = [
            _COMPOSE_NAME.get(svc, svc)
            for svc in resolved
            if resolved[svc].get("mode") == "local"
        ]
        gpu = any(svc in ("llm", "embedding")
                  for svc in up_services)
    else:
        # Legacy path: hardcoded up_services list + GPU probe.
        up_services = ["qdrant", "neo4j", "embedding-model"]
        # GPU probe (mirror of bootstrap-local.sh: actually RUN `nvidia-smi
        # -L` and check for non-empty output — the binary can exist without
        # a working driver/GPU, and the no-GPU path must skip the bundled
        # llm service, not waste a 300s health poll on it).
        gpu = _gpu_present()
        if gpu:
            up_services.append("llm")
    echo(f"starting local stack: {', '.join(up_services)}")
    # Pass the chosen credentials to compose (matches the compose-file
    # defaults unless the user overrode them).
    up_out: list = []
    rc = _docker_compose(
        ["up", "-d", *up_services],
        extra_env={"NEO4J_USER": neo4j_user,
                   "NEO4J_PASSWORD": neo4j_password},
        output=up_out)
    if rc != 0:
        if up_out:
            echo("docker compose up reported errors (last lines):")
            for line in up_out[0].splitlines():
                echo(f"  {line}")
        echo("docker compose up failed — some services may already be up "
             "from a previous run; checking health anyway.")
        # Do NOT give up: the stack may be partially up. Fall through to
        # the health poll; if the mandatory services answer we still write
        # kb.local.yml so the host config points at what is actually up.
        up_failed = True
    else:
        up_failed = False

    # Health-poll each service and report them individually — one line per
    # service with service-specific remediation, so the user sees exactly
    # which of qdrant / neo4j / llm / embedding-model is down.
    _MANDATORY_REMEDY = {
        "qdrant": ("run 'docker compose logs qdrant' to inspect the "
                   "container, free port 6333 if something else owns "
                   "it, then re-run 'digital-twins setup' to finish."),
        "neo4j": ("run 'docker compose logs neo4j' to inspect the "
                  "container; if the container was started with "
                  "different credentials, re-run setup and enter the "
                  "same credentials it was started with."),
    }
    failed = []
    for svc in ("qdrant", "neo4j"):
        if _poll_url(_HEALTH_URLS[svc]):
            echo(f"{svc}: healthy")
        else:
            failed.append(svc)
            echo(f"{svc}: FAILED the health check -> "
                 f"{_MANDATORY_REMEDY[svc]}")
    if gpu:
        if _poll_url(_HEALTH_URLS["llm"]):
            echo("llm: healthy")
        else:
            echo("llm: FAILED the health check -> point KB_LLM__ENDPOINT "
                 "at an external LLM; the rest of the stack is kept.")
    if _poll_url(_HEALTH_URLS["embedding-model"]):
        echo("embedding-model: healthy")
    else:
        echo("embedding-model: FAILED the health check -> embedding "
             "falls back to the in-process default; the rest of the "
             "stack is kept.")
    if failed:
        if up_failed:
            echo("remediation: some services are up (re-run setup to "
                 "finish); run 'docker compose logs <service>' for the "
                 "failed ones.")
        else:
            echo("remediation: fix the failed service(s) above, then "
                 "re-run 'digital-twins setup'.")
        return False
    if up_failed:
        echo("some services failed to start but the mandatory ones are "
             "healthy — continuing with the config that points at what "
             "is up.")

    if resolved is not None:
        # T1.3: write kb.local.yml from the resolved map so mixed
        # local/external choices land in one file.  Local services use
        # the bundled endpoint constants (already in resolved["url"]);
        # external services use the URL the user/env supplied (an empty
        # URL on a required service was already gated earlier).  The
        # neo4j entry carries user/password from the prompts.
        content = {
            "qdrant": {"url": resolved["qdrant"]["url"]},
            "neo4j": {"url": resolved["neo4j"]["url"],
                      "user": neo4j_user,
                      "password": neo4j_password},
            "llm": {"endpoint": resolved["llm"]["url"]},
            "embedding": {"endpoint": resolved["embedding"]["url"]},
        }
        # An external service whose URL is empty can't go in the file
        # (kb.local.yml would point at nothing); drop the key instead
        # and point the user at the env var that fills it.
        for svc, key in (("qdrant", "url"), ("neo4j", "url"),
                         ("llm", "endpoint"),
                         ("embedding", "endpoint")):
            if not resolved[svc].get("url"):
                content.pop(svc, None)
                echo(f"{svc}: URL not set — set "
                     f"{resolved[svc].get('env', 'KB_*')} to enable it.")
        if resolved["llm"].get("unavailable"):
            echo("llm: requested local but no suitable GPU — set "
                 "KB_LLM__ENDPOINT to an external LLM to enable chat")
    else:
        content = _kb_local_content(local=True,
                                    neo4j_user=neo4j_user,
                                    neo4j_password=neo4j_password)
        # On no-GPU hosts the bundled llm/embedding are not part of the
        # stack: drop them so kb.local.yml doesn't point at ports that
        # were never up.
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

def run_cloud_env_stack(echo: Callable = click.echo) -> bool:
    """Non-interactive cloud setup (--cloud-env): resolve the endpoints
    from the KB_* env vars alone, never prompt.

    A missing (or empty) required var fails with exit 5 and a
    remediation that names exactly which vars are unset/empty — the
    command is safe to run under a pipe or in CI (no stdin reads).
    """
    qdrant = os.environ.get("KB_QDRANT__URL", "").strip()
    neo4j = os.environ.get("KB_NEO4J__URL", "").strip()
    llm = os.environ.get("KB_LLM__ENDPOINT", "").strip()
    embedding = os.environ.get("KB_EMBEDDING__ENDPOINT", "").strip()
    neo4j_user = os.environ.get("KB_NEO4J__USER", "").strip()
    neo4j_password = os.environ.get("KB_NEO4J__PASSWORD", "").strip()
    missing = [name for name, value in (
        ("KB_QDRANT__URL", qdrant), ("KB_NEO4J__URL", neo4j),
        ("KB_LLM__ENDPOINT", llm)) if not value]
    if missing:
        echo("ERROR: cloud endpoints must be non-empty — "
             f"{' and '.join(missing)} "
             f"{'is' if len(missing) == 1 else 'are'} "
             "unset or empty. Set the var(s) and re-run "
             "'digital-twins setup --cloud-env'; this mode never prompts.")
        return False
    write_kb_local(_cloud_content(qdrant, neo4j, llm,
                                  embedding, neo4j_user, neo4j_password))
    echo("cloud mode (--cloud-env): no Docker required, no prompts — "
         "kb.local.yml now points at the cloud endpoints from the "
         "KB_* env vars.")
    return True


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
# fs demo source (the README fast-path's final step)
# ---------------------------------------------------------------------------

# The two sample files the README demo ("run 1 reports fs: 2 item(s)")
# relies on. Created in <config_dir>/kb-demo/ so the demo is self-contained
# and never writes outside the config dir.
_DEMO_DIR_NAME = "kb-demo"
_DEMO_FILES = {
    "welcome.md":
        "# welcome\n"
        "This is a sample file so `digital-twins run --source fs` has "
        "something to ingest on a fresh install. Replace these files with "
        "your own, or point sources.fs.extra.dir at a real directory.\n",
    "getting-started.md":
        "# getting started\n"
        "Second sample file. A second run of `digital-twins run --source "
        "fs` reports 0 new items — dedup-safe.\n",
}


def _ensure_demo_files(demo_dir: Path) -> None:
    demo_dir.mkdir(parents=True, exist_ok=True)
    for name, content in _DEMO_FILES.items():
        path = demo_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def configure_fs_demo(echo: Callable = click.echo,
                      _local_config_path=local_config_path,
                      _merge_write=None) -> None:
    """Make `digital-twins run --source fs` work out of the box.

    - Creates a demo dir (<config dir>/kb-demo/) with two sample .md files.
    - Adds a `sources.fs` block (enabled=true, extra.dir pointing at the
      demo dir) to kb.local.yml — writing the file if it does not exist
      yet, and merging into it when it does (existing keys preserved).
    - If the user already configured a `sources.fs` block (their own dir),
      it is left untouched — setup never overrides a user's source config.

    Idempotent: a re-run of setup never clobbers an existing sources.fs.
    """
    if _merge_write is None:
        from .config.local_io import merge_write
        _merge_write = merge_write

    path = Path(_local_config_path()).expanduser()
    data = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fs = (data.get("sources") or {}).get("fs") or {}
    if fs:
        # User already configured the fs source — honour their config.
        # Only override the demo-dir default when the user has not
        # explicitly enabled a dir of their own: an enabled block with a
        # different dir is the user's choice; a disabled block is the user
        # declining the demo. Either way, honour their config.
        echo("kb.local.yml already has a sources.fs block — leaving it "
             "untouched.")
        return
    config_dir = path.parent
    demo_dir = config_dir / _DEMO_DIR_NAME
    _ensure_demo_files(demo_dir)
    try:
        _merge_write(
            {"sources": {"fs": {"enabled": True,
                                 "extra": {"dir": str(demo_dir)}}}},
            target=path)
    except (ValueError, PermissionError, OSError) as exc:
        echo(f"warning: could not enable the fs demo source "
             f"({exc}) — enable it manually in kb.local.yml "
             f"when you are ready.")
        return
    echo(f"enabled the fs demo source (dir: {demo_dir}).")
# Interactive second pass (install-setup-separation T2.1)
# ---------------------------------------------------------------------------

def _interactive_second_pass(prompt: Callable, echo: Callable,
                             gpu: bool,
                             named: dict | None = None) -> dict:
    """Ask, per service not resolved from a flag, local-or-external.

    The interactive path (D2): after the user accepts the bundled local
    stack, the second pass asks, for each of qdrant/neo4j/llm/embedding,
    "local or external?" — with defaults qdrant/neo4j -> local and
    llm/embedding -> external on no-GPU hosts, local on GPU hosts.  An
    external answer prompts for the URL, or reads the KB_* env var when
    set (env wins over the prompt; a var set to an empty string is
    "set but empty" and fails the required check, like the cloud path).

    ``named`` is the set of services already resolved from ``--backends``
    (or the all-services shorthands); those never re-prompt — they keep
    their flag/env resolution.  ``named=None`` means the bare
    default-interactive path (docker probe -> local-stack confirm).

    Returns the resolved map in ``resolve_backends``() shape.  A
    "local" answer for a service the host can't start (llm/embedding on
    a no-GPU host) is flipped to external-with-unavailable by
    resolve_backends(), so it cannot crash — the existing
    run_local_stack() remediation behavior applies.

    ``echo`` is accepted for API symmetry with the other wizard callables
    (narration rides on the prompt text itself); the pass does not echo
    standalone lines.
    """
    named = named or {}
    explicit: dict = dict(named)  # flag-resolved services carry through
    base = resolve_backends(explicit if explicit else None, gpu=gpu,
                           docker=True)
    for svc, (env_name, _local_url, _gpu_only, _required) in \
            _SERVICE_ENV.items():
        if svc in named:
            continue  # resolved from the flag — never re-prompt
        default_mode = ("external"
                        if svc in ("llm", "embedding") and not gpu
                        else "local")
        answer = prompt(
            f"{svc}: run locally (bundled Docker) or point at an external "
            f"endpoint? [default: {default_mode}]").strip().lower()
        if answer in ("local", "l"):
            mode = "local"
        elif answer in ("external", "ext", "url", "e"):
            mode = "external"
        else:
            mode = default_mode  # empty answer (Enter) -> the default
        entry = base[svc]
        if mode == "local" and not entry.get("unavailable"):
            # Local wins: use the bundled endpoint (resolve_backends may
            # have flipped llm/embedding to unavailable on no-GPU hosts —
            # keep that flip, the user must pick external there).
            explicit[svc] = "local"
        else:
            # External: env var wins over the prompt; a set-but-empty
            # var fails the required check like the cloud path does.
            env_value = os.environ.get(env_name)
            if env_value is not None:
                url = env_value.strip()
            else:
                url = prompt(
                    f"External {svc} URL "
                    f"(e.g. https://host/v1) "
                    f"[{env_name}]: ").strip()
            explicit[svc] = url
    resolved = resolve_backends(explicit, gpu=gpu, docker=True)
    # Preserve the no-GPU "local unavailable" marker when the second pass
    # chose external for a local-only service (resolve_backends only sets
    # it on the auto local->external flip; an explicit external answer
    # keeps the marker so the remediation hint still fires):
    for svc in ("llm", "embedding"):
        if not gpu and base[svc].get("unavailable"):
            resolved[svc]["unavailable"] = True
    return resolved


# ---------------------------------------------------------------------------
# Top-level wizard
# ---------------------------------------------------------------------------

def run_setup(prompt: Callable = click.prompt,
              confirm: Callable = click.confirm,
              echo: Callable = click.echo,
              force_cloud: bool = False,
              skip_services: bool = False,
              cloud_env: bool = False,
              local: bool = False,
              backends: dict | None = None,
              gpu: bool | None = None) -> int:
    """Run the full setup wizard. Returns the process exit code.

    force_cloud: skip docker detection, go straight to the cloud path.
    cloud_env: non-interactive cloud setup — the endpoints must come
    from the KB_* env vars, never a prompt; a missing var exits 5
    naming the var (safe under a pipe or in CI).
    skip_services: explicit "I handle backends myself" — takes precedence
    over everything: never probe Docker, never prompt for endpoints, never
    write kb.local.yml; only init + admin + validate run (the re-run fast
    path). The fs-demo enable step is also skipped: no config file or
    demo dir is created as a side effect.  ``--skip-services`` combined
    with a non-empty ``--backends`` map is a contradiction: it errors
    out naming both flags (exit 1).
    local / backends (install-setup-separation T1.2): per-service backend
    choices.  ``local`` is the ``--local`` shorthand (all four services
    local); ``backends`` is the parsed ``--backends`` map.  Precedence
    (highest wins): --skip-services > --cloud-env > --cloud >
    --local/--backends > interactive.  The chosen services are resolved
    via resolve_backends() and threaded into run_local_stack(resolved=...)
    so mixed local/external choices start only the local services in
    Docker and write one kb.local.yml.
    """
    # --- 0) contradiction gate ---------------------------------------------
    if skip_services and backends:
        echo("ERROR: --skip-services and --backends contradict each other: "
             "--skip-services tells setup to never start or configure "
             "services, while --backends specifies per-service backends. "
             "Drop one of the two flags and re-run.")
        return 1

    # --- 1) backend mode ---------------------------------------------------
    try:
        if skip_services:
            echo("skipping service startup (--skip-services).")
        elif cloud_env:
            if not run_cloud_env_stack(echo):
                return 5
        elif force_cloud:
            if not run_cloud_stack(prompt, echo):
                return 5
        elif local or backends:
            # T1.2: per-service backend choices.  Resolve the map (the
            # gpu/docker facts come from the host probes) and dispatch:
            # if any service is local the local stack path runs (only the
            # local services start in Docker; kb.local.yml is written
            # from the resolved map); if every service is external the
            # cloud path runs instead.
            if gpu is None:
                gpu = _gpu_present()
            resolved = resolve_backends(backends, local=local,
                                         gpu=gpu,
                                         docker=docker_available())
            has_local = any(v.get("mode") == "local"
                            for v in resolved.values())
            # A required service with an empty URL (the "" value of
            # --backends, i.e. "required-external, URL not supplied"):
            # no prompt in flag mode — exit 5 naming the env var that
            # fills it.  (Not gated for --local mode: an unavailable
            # local LLM on a no-GPU host is handled by run_local_stack,
            # which skips it with a remediation hint.)
            if not local:
                missing = [v["env"] for v in resolved.values()
                           if v.get("mode") == "external"
                           and not v.get("url")
                           and v.get("required")]
                if missing:
                    echo("ERROR: the following required backend(s) have no "
                         "URL — "
                         f"{' and '.join(missing)} "
                         "is unset or empty. Set the var(s) (or pass a URL "
                         "via --backends) and re-run 'digital-twins setup'.")
                    return 5
            if has_local:
                local_svcs = ", ".join(svc for svc in resolved
                                       if resolved[svc].get("mode") == "local")
                echo(f"starting local stack for: {local_svcs} "
                     "(per --local/--backends); external services use "
                     "the URLs you supplied.")
                if not run_local_stack(prompt, echo, resolved=resolved):
                    return 3
            else:
                # All four services external: the cloud path writes the
                # endpoints (no docker, no local stack).
                echo("all backends are external — running the cloud path "
                     "(no Docker, no local stack).")
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
            # T2.1: interactive second pass — after the user accepted the
            # bundled local stack, ask, per service, local-or-external
            # (defaults: qdrant/neo4j local; llm/embedding local on GPU
            # hosts, external on no-GPU hosts).  This branch is the bare
            # default-interactive path: no --local/--cloud/--cloud-env,
            # no --skip-services, and no service named in --backends, so
            # every service is open to the per-service prompt (``named``
            # is empty).  A "local" answer the host can't start
            # (llm/embedding without a GPU) is flipped to
            # external-with-unavailable by resolve_backends() —
            # run_local_stack() then applies its existing remediation
            # instead of crashing.  The resolved map drives
            # run_local_stack (T1.3):
            # only the local-mode services start in Docker and
            # kb.local.yml is written from the resolved map.
            if gpu is None:
                gpu = _gpu_present()
            resolved = _interactive_second_pass(
                prompt, echo, gpu=gpu, named=None)
            if not run_local_stack(prompt, echo, resolved=resolved):
                return 3
        else:
            # No docker, or the user declined the local stack: fall back
            # to cloud mode.
            echo("falling back to cloud mode.")
            if not run_cloud_stack(prompt, echo):
                return 5
    except (EOFError, KeyboardInterrupt, click.exceptions.Abort):
        # An interrupted prompt: Ctrl-C (KeyboardInterrupt), EOF on
        # non-interactive stdin (raw EOFError), or click's Abort —
        # click.confirm()/click.prompt() raise Abort (not EOFError) when
        # stdin is a closed pipe, which is exactly the curl|bash case.
        # All three are a user cancellation, not a failed health check:
        # report a clean cancellation instead of leaking the 'Aborted!'
        # message and exit 1.
        echo("setup was interrupted before the backend was configured — "
             "re-run 'digital-twins setup' to continue (or set the "
             "KB_* env vars and re-run with --cloud-env).")
        return 6

    # --- 2) init (state DB + migrations, no endpoint prompts) --------------
    # connect() already migrates on open (state.db.connect); no second
    # migrate call needed.  The real state.db.connect creates the state
    # directory itself; a test double (or any narrower connect) may not,
    # so make sure it exists first.
    cfg = load()
    state_dir = Path(cfg["state_dir"]).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
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

    # --- 5) enable the fs demo source (the README fast path's last step) ----
    # --skip-services means "I handle backends and sources myself": the
    # wizard must not create or modify a config file or a demo dir as a
    # side effect. Otherwise (normal / re-run / valid-config paths), the
    # machine config layer exists (or is about to be created below by the
    # cloud/local path that just ran) and the fs-demo source is enabled so
    # the fast path's last command works out of the box.  Note: this runs
    # even when a health check above failed (exit code 1) — enabling the
    # demo source is best-effort and independent of backend health: the
    # config merge itself either succeeds or prints a remediation line,
    # and any re-run of setup skips this step (the sources.fs block is
    # already present, so configure_fs_demo is a no-op).
    if not skip_services:
        echo("")
        echo("enabling the fs demo source so "
             "'digital-twins run --source fs' works out of the box")
        configure_fs_demo(echo=echo)

    # --- 6) next steps ------------------------------------------------------
    echo("")
    echo("Next: run your first ingest:")
    echo("  digital-twins run --source fs")
    echo("  (put your own .md files in the demo dir to replace the "
         "samples, or point sources.fs.extra.dir elsewhere.)")
    return exit_code
