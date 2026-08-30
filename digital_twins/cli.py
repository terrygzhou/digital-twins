"""digital-twins command-line interface."""

import hmac
import os
from pathlib import Path
import uuid

import click
import yaml

from digital_twins import __version__, health
from digital_twins.config import ConfigError, load
from digital_twins.config.loader import _merge
from digital_twins.config.schema import BUILTIN_SOURCES, get
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.state.models import finish_audit_run, start_audit_run

# knobs init resolves: path, prompt label, hide input
_ENDPOINT_PROMPTS = (
    ("qdrant.url", "Qdrant URL (e.g. https://host:6333)", False),
    ("neo4j.url", "Neo4j URL (e.g. bolt://host:7687)", False),
    ("neo4j.user", "Neo4j user", False),
    ("neo4j.password", "Neo4j password", True),
    ("llm.endpoint", "LLM endpoint (OpenAI-compatible base URL)", False),
    ("llm.model", "LLM model name", False),
)

_ENDPOINT_SECTIONS = (
    ("qdrant", ("url", "api_key")),
    ("neo4j", ("url", "user", "password")),
    ("llm", ("endpoint", "model", "api_key")),
)


def pre_command() -> None:
    """Load config and complete DB migrations before any command runs.

    Fresh install (no state dir yet) => nothing to migrate; `init` creates
    it. Invalid config fails fast with the offending knob named (SC-001).
    """
    try:
        cfg = load()
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}")
    state_dir = Path(cfg["state_dir"])
    if state_dir.is_dir():
        conn = connect(state_dir)
        try:
            migrate(conn)
        finally:
            conn.close()


def _ensure_first_admin(conn) -> None:
    """Create the first admin account if the accounts table is empty (C-4/R7).

    003's first-admin step: called by ``init`` after the state DB is
    migrated, so the ``accounts`` table exists.  Idempotent — a re-run of
    ``init`` never creates a second admin (US1 S1):

    - ``accounts`` empty: read ``INIT_ADMIN_EMAIL`` /
      ``INIT_ADMIN_PASSWORD`` (auth-only env vars, same pattern as 002's
      ``DT_USER_PASSWORD``: read from ``os.environ`` directly, never a
      config knob, never in argv, never echoed to logs) and, if the env
      vars are missing, prompt interactively for email + password.
      Create the first account via the shared ``create_account`` helper
      (R7: first row in ``accounts`` -> ``admin``).  Echo
      "created first admin account ``<email>``".
    - ``accounts`` non-empty: create nothing; echo
      "admin already exists: ``<first-email>``" and skip.
    """
    from digital_twins.accounts import create_account

    first = conn.execute(
        "SELECT email FROM accounts ORDER BY rowid LIMIT 1").fetchone()
    if first is not None:
        click.echo(f"admin already exists: `{first[0]}`")
        return

    email = os.environ.get("INIT_ADMIN_EMAIL")
    password = os.environ.get("INIT_ADMIN_PASSWORD")
    if email is None:
        email = click.prompt("First admin email")
    if password is None:
        password = click.prompt("First admin password", hide_input=True)
    if not email or not password:
        click.echo("first admin credentials must be non-empty", err=True)
        raise SystemExit(2)
    create_account(conn, email, password)
    click.echo(f"created first admin account `{email}`")


def _print_report(results) -> None:
    for r in results:
        line = (f"{r.endpoint:<9} {'ok' if r.ok else 'FAIL':<5} "
                f"{(r.status or '-'):<13} {r.detail}")
        if not r.ok:
            line += f"  -> {r.remediation}"
        click.echo(line)


def _exit_code(results) -> int:
    return 1 if any(not r.ok for r in results) else 0


# IMAP mail sources: credential env var + account-address env var
_IMAP_ENV = {
    "yahoo": ("YMAIL_APP_PASSWORD", "YMAIL_EMAIL"),
    "gmail": ("GMAIL_APP_PASSWORD", "GMAIL_EMAIL"),
}


def _starter(overrides: dict, cfg: dict) -> dict:
    """Starter kb.local.yml: resolved endpoints + every built-in source disabled.

    IMAP mail sources (yahoo/gmail) additionally ship their `credential`
    env-var name and an empty `email` knob so the user knows both vars exist.
    """
    sources: dict = {
        name: {"enabled": False, "max_items": 200, "timeout_s": 1500}
        for name in BUILTIN_SOURCES
    }
    for name, (cred, _email_env) in _IMAP_ENV.items():
        sources[name]["credential"] = cred
        sources[name]["email"] = ""
    data: dict = {"sources": sources}
    for prefix, keys in _ENDPOINT_SECTIONS:
        values = {}
        for key in keys:
            value = overrides.get(f"{prefix}.{key}") or get(cfg, f"{prefix}.{key}")
            if value:
                values[key] = value
        if values:
            data[prefix] = values
    return data


def _cli_callback(ctx, version_json: bool) -> None:
    """Group callback: handle --version-json before Click's version_option fires."""
    if version_json:
        import json
        click.echo(json.dumps({"name": "digital-twins", "version": __version__}))
        ctx.exit(0)
    pre_command()


@click.group(invoke_without_command=True)
@click.version_option(version=__version__)
@click.option("--version-json", is_flag=True, default=False,
              help="Print machine-readable version (JSON) and exit.")
@click.pass_context
def cli(ctx: click.Context, version_json: bool) -> None:
    """Environment-portable KB ingestion: layered config, fail-fast sources, dedup-safe ingest."""
    _cli_callback(ctx, version_json)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        raise SystemExit(0)


@cli.command()
def validate() -> None:
    """Health-check the configured endpoints. Exits 0 only when all pass."""
    results = health.run_health_checks(load())
    _print_report(results)
    raise SystemExit(_exit_code(results))


@cli.command()
@click.option("--source", "source_names", multiple=True,
              help="Only these source names (they must be enabled in config).")
@click.option("--max-items", type=int, default=None,
              help="Override the per-source item cap for this run.")
@click.option("--dry-run", is_flag=True,
              help="Count what would be ingested; write nothing.")
@click.option("--once", is_flag=True,
              help="One-shot run: trigger='manual', no schedule advance, no pidfile.")
@click.option("--as", "as_user", type=str, default=None,
              help="Run as this user (requires --once). Password comes from "
                   "the DT_USER_PASSWORD env var; auth failure exits 2 "
                   "without touching any state.")
def run(source_names: tuple, max_items: int, dry_run: bool,
        once: bool, as_user: str) -> None:
    """One-shot ingestion: read -> chunk -> embed -> upsert.

    Fail-fast: a missing prerequisite exits 2 and names the source, the
    prerequisite, and where to set it. An audit row is written regardless.

    --once marks the run as one-shot (host-cron path): trigger='manual',
    scheduled_by='system', no schedule advance, no pidfile. Without --once,
    behavior is unchanged from 001 (the same defaults apply: the CLI one-shot
    command records trigger='manual' / scheduled_by='system').

    --as USER authenticates against the accounts store (pbkdf2, R-04) using
    the DT_USER_PASSWORD env var and records scheduled_by=USER. Auth runs
    BEFORE any pipeline work: a bad password exits 2 without writing an
    audit row, touching Qdrant, or advancing high-water. v1 reads the
    password from the env var only (no interactive prompt — 003 territory).
    """
    # --as: authenticate BEFORE any pipeline work. The check precedes config
    # load, state-dir creation, and db connect, so a failure exits 2 without
    # touching any state (ruling: "bad password exits 2 without touching any
    # state"). DT_USER_PASSWORD is an auth-only env var (ruling R-06): read
    # from os.environ directly, NOT a config knob.
    if as_user is not None:
        if not once:
            click.echo("--as requires --once", err=True)
            raise SystemExit(2)
        password = os.environ.get("DT_USER_PASSWORD")
        if password is None:
            click.echo("DT_USER_PASSWORD not set", err=True)
            raise SystemExit(2)
        from digital_twins.auth import authenticate
        cfg = load()
        state_dir = Path(cfg["state_dir"])
        if not state_dir.is_dir():
            # "no state db" is a setup condition, not a credential failure:
            # the accounts store is missing, so authentication could not be
            # attempted. Naming the fix ("run init") keeps this distinct from
            # a bad-password "authentication failed" below (T020 deferred-minor
            # from the T011 review).
            click.echo(
                f"cannot authenticate as '{as_user}': no state db at "
                f"{state_dir} — run 'digital-twins init' first", err=True)
            raise SystemExit(2)
        db = connect(state_dir)
        try:
            ok = authenticate(db, as_user, password)
            if not ok:
                click.echo(f"authentication failed for '{as_user}'", err=True)
                raise SystemExit(2)
            # 003 post-auth role check (C-2 / R3): after authentication,
            # verify the caller's role permits trigger_run BEFORE any
            # pipeline work. On denial: exit 2, named reason, no audit row,
            # no Qdrant write, no high-water advance (same fail-fast
            # contract as 002's bad-password path).
            from digital_twins.accounts import get_role, require_capability, RoleDenied
            caller_role = get_role(db, as_user)
            if caller_role is None:
                # Authenticated but the account no longer exists (race:
                # account deleted between authenticate and get_role).
                click.echo(f"authentication failed for '{as_user}'", err=True)
                raise SystemExit(2)
            try:
                require_capability(caller_role, "trigger_run", "trigger a run")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)
        finally:
            db.close()
        # 003 C-3: merge the caller's per-user config overrides into a COPY of
        # the global config (never mutate the global). The merge happens at
        # the caller (CLI `run --as`), not in run_pipeline (constitution II).
        # The auth connection is closed; reopen a fresh one for the merge read.
        from digital_twins.user_config import merge_user_config
        db2 = connect(state_dir)
        try:
            cfg = merge_user_config(cfg, db2, as_user)
        finally:
            db2.close()
        run_owner = as_user
        scheduled_by = as_user
    else:
        run_owner = None
        scheduled_by = "system"

    # --once: one-shot host-cron run. trigger='manual', scheduled_by per
    # --as (or 'system'), no schedule advance, no pidfile. Without --once
    # the 001 behavior is unchanged. (T008's `serve` will use
    # trigger='schedule'.)
    trigger = "manual"
    if run_owner is None:
        # No --as: read global config fresh (001 behavior unchanged).
        cfg = load()
    # else: cfg is already the merged user-config copy from the --as branch above.
    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)

        from digital_twins.health import ServiceDependencyError
        from digital_twins.ingest.pipeline import (
            DimensionMismatchError,
            PrerequisiteError,
            run_pipeline,
        )
        from digital_twins.sources import (
            CustomSourceError,
            UnknownSourceError,
            build as build_source,
        )

        # fail-fast: source prerequisite check BEFORE any endpoint config
        names = list(source_names) if source_names else [
            n for n, e in cfg["sources"].items() if e.get("enabled")
        ]
        for name in names:
            entry = cfg["sources"].get(name)
            if entry is None:
                raise UnknownSourceError(name)
            source = build_source(name, entry)
            missing = source.prerequisites()
            if missing:
                run_id = str(uuid.uuid4())
                start_audit_run(db, run_id)
                finish_audit_run(db, run_id, "failed", {})
                click.echo(
                    f"fail-fast: source '{name}': "
                    f"missing prerequisite(s): {'; '.join(missing)}",
                    err=True)
                raise SystemExit(2)
            source.close()

        qdrant_url = get(cfg, "qdrant.url")

        def qdrant_factory():
            if not qdrant_url:
                raise ConfigError(
                    "qdrant.url is not set — run init or set KB_QDRANT__URL")
            from qdrant_client import QdrantClient
            return QdrantClient(
                url=qdrant_url,
                api_key=get(cfg, "qdrant.api_key") or None)

        qdrant = None if dry_run else qdrant_factory
        embedder = None if dry_run else _make_embedder(cfg)

        try:
            summary = run_pipeline(
                cfg, db, qdrant, embedder,
                source_names=list(source_names) or None,
                max_items=max_items, dry_run=dry_run,
                trigger=trigger, scheduled_by=scheduled_by,
                owner=run_owner)
        except PrerequisiteError as exc:
            click.echo(f"fail-fast: {exc}", err=True)
            raise SystemExit(2)
        except ServiceDependencyError as exc:
            click.echo(f"fail-fast: {exc}", err=True)
            raise SystemExit(2)
        except DimensionMismatchError as exc:
            click.echo(f"dimension mismatch: {exc}", err=True)
            raise SystemExit(1)
        except (ConfigError, UnknownSourceError, CustomSourceError) as exc:
            raise SystemExit(f"config error: {exc}")

        for name in sorted(summary.counts):
            click.echo(f"{name}: {summary.counts[name]} item(s)")
        mode = "would ingest" if dry_run else "ingested"
        click.echo(f"{mode} {summary.points} point(s) — run_id {summary.run_id}")
    finally:
        db.close()


def _make_embedder(cfg):
    from digital_twins.ingest.embedding import load_embedder, build_endpoint_embedder

    # FR-003: endpoint-aware embedder when embedding.endpoint is set,
    # in-process pinned model otherwise (additive, unchanged default).
    if get(cfg, "embedding.endpoint"):
        return build_endpoint_embedder(cfg)

    state = {}

    def embed(texts):
        if "model" not in state:
            state["model"] = load_embedder(
                get(cfg, "embedding.model"),
                get(cfg, "embedding.device") or "auto")
        return state["model"].encode(list(texts)).tolist()

    return embed


@cli.command()
@click.option(
    "--yes", is_flag=True,
    help="Do not prompt: keep existing values, leave missing endpoints unset.")
def init(yes: bool) -> None:
    """First-run setup: endpoints, starter kb.local.yml, state DB, health report.

    Idempotent: existing values are kept, only missing pieces are prompted
    or added; an interrupted init can be re-run safely.
    """
    cfg = load()
    overrides: dict = {}
    for path, label, hide in _ENDPOINT_PROMPTS:
        if get(cfg, path) or yes:
            continue
        overrides[path] = click.prompt(label, hide_input=hide)

    config_dir = Path(cfg["config_dir"])
    config_dir.mkdir(parents=True, exist_ok=True)
    starter_path = config_dir / "kb.local.yml"

    starter = _starter(overrides, cfg)
    if starter_path.is_file():
        existing = yaml.safe_load(
            starter_path.read_text(encoding="utf-8")) or {}
        starter = _merge(starter, existing)  # existing values win
    starter_path.write_text(
        yaml.safe_dump(starter, sort_keys=False), encoding="utf-8")

    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(state_dir)
    try:
        migrate(conn)
        # 003 first-admin step (C-4/R7): after the state DB is migrated,
        # create the first admin account if accounts is empty; idempotent
        # (a re-run of init never creates a second admin).
        _ensure_first_admin(conn)
    finally:
        conn.close()

    results = health.run_health_checks(load())
    _print_report(results)
    raise SystemExit(_exit_code(results))


def _auth_checker(db, service_token_env: str = "DT_SERVICE_TOKEN"):
    """Build the StatusServer ``auth_checker`` for the serve ``/status`` gate.

    003 C-5 / R8 (contracts/scheduler.md): accept, in order:
      1. the shared service token (BR-10) — ``DT_SERVICE_TOKEN`` env var,
         constant-time compare;
      2. a valid personal token (``personal_tokens`` table, T005);
      3. a valid session token (``sessions`` table, T006).
    Missing / no Bearer prefix -> ``False`` (401). A Bearer that matches
    none of the above -> ``False`` (401 — unknown credential; 003's
    ``/status`` is read-only, so there is no 403 to mean here). A known
    credential that is suspended would return a string (403) — reserved for
    the future mutating routes (004/005); in 003 every known credential is
    either accepted (200) or unknown (401).
    """
    def check(headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False  # 401: no credential
        token = auth[len("Bearer "):].strip()
        # 1) shared service token (BR-10, constant-time compare)
        service = os.environ.get(service_token_env)
        if service and hmac.compare_digest(token, service):
            return True
        # 2) personal token (personal_tokens table, T005)
        from digital_twins.auth import verify_personal_token
        if verify_personal_token(db, token):
            return True
        # 3) session token (sessions table, T006, TTL + revoked)
        from digital_twins.auth import verify_session
        if verify_session(db, token):
            return True
        # 4) a Bearer that matches none of the above: 401 (unknown) — not
        #    403, because there is no mutating /status route for a 403 to
        #    mean (contracts/scheduler.md).
        return False

    return check


@cli.command()
@click.option("--port", type=int, default=None,
              help="Override the status port. Defaults to the "
                   "scheduler.status_port knob; 0 disables the status "
                   "endpoint entirely.")
@click.option("--tick-seconds", type=float, default=None,
              help="Override the loop cadence (default from the loop "
                   "module; 5.0 is the documented default).")
def serve(port: int, tick_seconds: float) -> None:
    """Start the long-running scheduler (serve).

    Loads config, connects and migrates the state DB, then hands off to
    run_serve (T007).

    008 US1 AC3: serve now gates on hard service dependencies at
    startup — ``health.preflight`` runs before any state/loop work and a
    down or misconfigured service exits 2 naming the service + remediation
    (the MVP made all four services hard dependencies; 001's
    no-gate-at-startup note is superseded).  Fires that start while a
    dependency recovers mid-run are still reported, never silent —
    T006's ``serve_once_tick`` audits a ``failed`` row and advances
    (R-07).

    --port overrides scheduler.status_port; --port 0 disables the status
    endpoint entirely (no socket bound). Fails fast with exit code 2 if
    another live serve holds the pidfile (run_serve raises that; the CLI
    maps it to exit 2). SIGTERM/SIGINT -> clean shutdown (handled by
    run_serve).
    """
    cfg = load()

    # 008 US1 AC3: hard service dependencies are gated at startup — a down
    # or misconfigured service exits non-zero and names the fix, before
    # any state/loop work. (Overrides 001's no-gate-at-startup note.)
    try:
        health.preflight(cfg)
    except health.ServiceDependencyError as exc:
        click.echo(f"fail-fast: {exc}", err=True)
        raise SystemExit(2)

    # Determine the status port: --port if given, else the knob default.
    status_port = port if port is not None else get(cfg, "scheduler.status_port")

    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)

        from digital_twins.scheduler.loop import run_serve

        # Build the status server only if the port is > 0 AND T017 has
        # landed. status.py is an empty skeleton until T017; the lazy
        # import + try/except lets serve work with --port 0 (or any port)
        # before T017 lands, and warn rather than crash.
        status_server = None
        if status_port > 0:
            try:
                from digital_twins.scheduler.status import StatusServer
                # 003 C-5 / R8: gate /status on the auth_checker (service
                # token -> personal token -> session token; else 401).
                checker = _auth_checker(db)
                status_server = StatusServer(
                    ("127.0.0.1", status_port), db, cfg,
                    auth_checker=checker)
            except ImportError:
                click.echo(
                    f"serve: status server not yet available (port "
                    f"{status_port}); the status endpoint will be "
                    f"unavailable until T017 lands", err=True)

        try:
            run_serve(
                db, cfg, status_port,
                status_server=status_server,
                tick_seconds=(5.0 if tick_seconds is None else tick_seconds),
            )
        except SystemExit as exc:
            # run_serve raises SystemExit with a message (not an int code)
            # on the pidfile guard (R4: "serve already running: pid N holds
            # <lock>"). Map that to exit 2 (the fail-fast code) with the
            # message echoed to stderr.
            if exc.code is not None and exc.code != 0:
                click.echo(str(exc.code), err=True)
                raise SystemExit(2)
            raise
    finally:
        db.close()

    click.echo("serve: stopped (clean shutdown)")


@cli.command()
def web() -> None:
    """Start the web app (UI + /api/* REST surface).

    Loads config (pre_command), reads ``web.bind`` / ``web.port`` from the
    config layer, builds the WebApp, prints the listening URL, and serves
    on the main thread (``ThreadingHTTPServer.serve_forever``).

    This is a separate surface from ``serve``: it does NOT start the
    scheduler loop and does NOT write the scheduler pidfile.
    """
    pre_command()
    cfg = load()
    bind = get(cfg, "web.bind")
    port = int(get(cfg, "web.port"))

    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)
        from digital_twins.web.app import build_web_app
        app = build_web_app(db, cfg, host=bind, port=port)
        click.echo(
            f"web: listening on http://{bind}:{port} "
            f"(UI: http://{bind}:{port}/)")
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.shutdown()
            app.server_close()
    finally:
        db.close()


@cli.command("serve-mcp")
@click.option("--transport",
              type=click.Choice(["stdio", "http"], case_sensitive=False),
              default="stdio", show_default=True,
              help="MCP transport to run.")
@click.option("--port", type=int, default=None,
              help="Port for the http transport. Defaults to the "
                   "mcp.port knob; ignored for stdio.")
def serve_mcp(transport: str, port: int) -> None:
    """Start the MCP server (serve-mcp).

    Runs the MCP tool server on the chosen transport:

    - ``stdio`` — newline-delimited JSON on stdin/stdout. The process-owner
      credential (DT_SERVICE_TOKEN / DT_PERSONAL_TOKEN) identifies the
      caller for every request.
    - ``http`` — a minimal HTTP server on 127.0.0.1:<port> that accepts
      ``POST /mcp`` with a JSON body; per-request auth via the
      ``Authorization: Bearer`` header (service / personal / session
      token, in that order).

    The transport is a thin adapter: all logic (role-gate, owner-scope,
    tool bodies, audit) lives in ``digital_twins.mcp.dispatch``. R7 parity
    across transports holds by construction.
    """
    cfg = load()
    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)
        from digital_twins.config.schema import get as _get
        service_account_email = _get(cfg, "mcp.service_account_email")

        if transport == "stdio":
            from digital_twins.mcp import stdio
            click.echo("serve-mcp: stdio transport ready (stdin/stdout)",
                       err=True)
            try:
                stdio.main(db, service_account_email=service_account_email,
                           config=cfg)
            finally:
                click.echo("serve-mcp: stdio transport stopped", err=True)
        else:  # http
            mcp_port = port if port is not None else _get(cfg, "mcp.port")
            if mcp_port <= 0:
                click.echo(
                    f"serve-mcp: --port must be > 0 for the http transport "
                    f"(got {mcp_port})", err=True)
                raise SystemExit(2)
            from digital_twins.mcp import http as mcp_http
            click.echo(
                f"serve-mcp: http transport listening on 127.0.0.1:{mcp_port}",
                err=True)
            try:
                mcp_http.main(db, service_account_email=service_account_email,
                              port=mcp_port, config=cfg)
            except KeyboardInterrupt:
                pass
            finally:
                click.echo("serve-mcp: http transport stopped", err=True)
    finally:
        db.close()


# --- schedule group (T012, US3/US4 — 003 role-checked CRUD) -----------------
#
# 003 post-auth role check (C-2): every schedule subcommand authenticates
# first via DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as, then checks the
# caller's role against R3 (schedule_crud: admin/scheduler yes, reader no).
# On denial: exit 2, named reason, no schedule row written.
#
# --as semantics: for `add` and `remove`, --as names the account to
# authenticate (and the owner label for `add`). For `list`, --as is an
# optional filter (no auth required).


def _open_schedules_db() -> "sqlite3.Connection":
    """Load config, connect + migrate the state DB.

    Fresh install (no state dir yet) gets created + migrated here so a
    first ``schedule add`` on a clean install works without a prior
    ``init``.
    """
    cfg = load()
    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    migrate(db)  # idempotent: no-op if already migrated
    return db


def _schedule_authenticate(db, as_user: str) -> tuple[str, str]:
    """Authenticate the caller for schedule commands and resolve their role.

    Delegates to :func:`_token_authenticate` (the shared 003 auth helper).
    """
    return _token_authenticate(db, as_user)


@cli.group()
def schedule() -> None:
    """Manage scheduled runs (v1: add / list / remove)."""


@schedule.command("add")
@click.option("--source", required=True,
              help="Source name (must exist in config).")
@click.option("--preset", required=True,
              help="Cadence preset: daily, hourly, weekly, monthly, "
                   "every-N-hours.")
@click.option("--param", type=int, default=None,
              help="N hours for every-N-hours. Required for that preset; "
                   "ignored (and rejected) for all others.")
@click.option("--fire-time", default="03:00", show_default=True,
              help="Local time of day (HH:MM) to fire.")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as (and the owner label). "
                   "Requires DT_USER_PASSWORD or DT_PERSONAL_TOKEN.")
def schedule_add(source: str, preset: str, param: int, fire_time: str,
                 as_user: str) -> None:
    """Add a schedule (upserts on the 5-field key).

    003 post-auth role check: the caller must have the ``schedule_crud``
    capability (R3: admin/scheduler yes, reader no). On denial: exit 2,
    named reason, no schedule row written.
    """
    import sqlite3 as _sqlite3
    db = _open_schedules_db()
    try:
        # 003 post-auth role check (C-2 / R3)
        from digital_twins.accounts import require_capability, RoleDenied
        caller_email, caller_role = _schedule_authenticate(db, as_user)
        try:
            require_capability(caller_role, "schedule_crud", "manage schedules")
        except RoleDenied as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)

        from digital_twins.scheduler.schedules import create_schedule
        try:
            row = create_schedule(db, caller_email, source, preset,
                                  param, fire_time)
        except ValueError as exc:
            # Config-class error (FR-8): exit 2, list the valid presets so
            # the user can self-correct without consulting docs.
            from digital_twins.scheduler.presets import preset_values
            valid = ", ".join(preset_values())
            click.echo(f"invalid schedule: {exc}", err=True)
            click.echo(f"valid presets: {valid}", err=True)
            raise SystemExit(2)
        db.commit()  # the CLI is the transaction boundary (T012)
        click.echo(f"schedule id={row['id']} next_fire_at={row['next_fire_at']}")
    finally:
        db.close()


@schedule.command("list")
@click.option("--as", "as_user", default=None,
              help="Filter by owner label. Default: all owners.")
def schedule_list(as_user: str) -> None:
    """List schedules. ``--as`` filters by owner label (filter only, no auth)."""
    db = _open_schedules_db()
    try:
        from digital_twins.scheduler.schedules import list_schedules
        rows = list_schedules(db, as_user)
        if not rows:
            click.echo("no schedules")
            return
        header = ("id", "owner", "source", "preset", "param",
                  "fire_time", "next_fire_at", "enabled")
        widths = [max(len(h), *(len(str(r[c]) if r[c] is not None else "-")
                                 for r in rows)) for h, c in
                  zip(header, ("id", "owner", "source", "preset", "param",
                               "fire_time", "next_fire_at", "enabled"))]
        click.echo("  ".join(h.ljust(w) for h, w in zip(header, widths)))
        for r in rows:
            cells = [str(r[c]) if r[c] is not None else "-"
                     for c in ("id", "owner", "source", "preset", "param",
                               "fire_time", "next_fire_at", "enabled")]
            click.echo("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    finally:
        db.close()


@schedule.command("remove")
@click.option("--id", "schedule_id", type=int, required=True,
              help="Schedule id to remove.")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Requires DT_USER_PASSWORD "
                   "or DT_PERSONAL_TOKEN.")
def schedule_remove(schedule_id: int, as_user: str) -> None:
    """Remove a schedule by id. Exit 1 if the id does not exist.

    003 post-auth role check: the caller must have the ``schedule_crud``
    capability (R3: admin/scheduler yes, reader no).
    """
    db = _open_schedules_db()
    try:
        from digital_twins.accounts import require_capability, RoleDenied
        caller_email, caller_role = _schedule_authenticate(db, as_user)
        try:
            require_capability(caller_role, "schedule_crud", "manage schedules")
        except RoleDenied as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)

        from digital_twins.scheduler.schedules import delete_schedule
        existing = db.execute(
            "SELECT 1 FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if existing is None:
            click.echo(
                f"schedule id {schedule_id} not found", err=True)
            raise SystemExit(1)
        delete_schedule(db, schedule_id)
        db.commit()  # the CLI is the transaction boundary (T012)
        click.echo(f"removed schedule id={schedule_id}")
    finally:
        db.close()


@cli.command()
@click.option("--email", required=True,
              help="Account email address.")
@click.option("--password", required=True,
              help="Account password (stored hashed, never in plaintext).")
def signup(email: str, password: str) -> None:
    """Create an account.

    Role is resolved by the shared helper: the first row in ``accounts``
    becomes ``admin``, every later account is a ``reader`` (C-4/R7).
    Duplicate email exits 2 with "account already exists: ``<email>``"
    and leaves no second row.  The password is hashed with 001's
    ``hash_password`` (R1); ``created_at``/``last_active`` are set to now.
    """
    from digital_twins.accounts import DuplicateEmailError, create_account

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)
        try:
            create_account(db, email, password)
        except DuplicateEmailError:
            click.echo(f"account already exists: `{email}`", err=True)
            raise SystemExit(2)
        role = "admin" if _count_accounts(db) == 1 else "reader"
        click.echo(f"created account `{email}` (role: {role})")
    finally:
        db.close()


def _count_accounts(db) -> int:
    """Number of rows in the accounts table."""
    return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


# --- token group (T010, US3 — personal token self-service) -------------------
#
# All three subcommands (create, list, revoke) authenticate first via
# DT_PERSONAL_TOKEN (the personal token resolves to an account_email +
# role), then check the caller's role against R3 (manage_own_tokens).
#
# Authentication: DT_PERSONAL_TOKEN env var (auth-only, read from
# os.environ, never in argv, never in logs). When set, the token is
# verified via auth.verify_personal_token; the resolved account_email
# becomes the caller's identity. If DT_PERSONAL_TOKEN is missing or
# invalid, exit 2 with a named reason.
#
# Role checks (R3 / contracts/cli.md):
# - token create (no --as): self-service; any authenticated role with
#   manage_own_tokens (admin/scheduler/reader).
# - token create --as USER: admin-only (manage_accounts-adjacent: create
#   for another user). The caller must be admin; the target user must
#   exist.
# - token list (no --as): self: own tokens; admin: all.
# - token list --as USER: admin-only: list a specific user's tokens.
# - token revoke --id N: self: own tokens only; admin: any. The caller
#   must own the token (or be admin).


def _token_authenticate(db, as_user: str = None) -> tuple[str, str]:
    """Authenticate the caller via DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as.

    Returns ``(account_email, role)`` on success. Exits 2 with a named
    reason on failure.

    Auth path:
    - ``DT_PERSONAL_TOKEN`` set: verify the token → resolve account_email + role.
    - Else if ``as_user`` is provided: ``--as USER`` + ``DT_USER_PASSWORD``
      → authenticate against the accounts store, then resolve the role.
    - Else: exit 2 with a named reason (no credentials).
    """
    from digital_twins.auth import verify_personal_token
    from digital_twins.accounts import get_role

    token = os.environ.get("DT_PERSONAL_TOKEN")
    if token:
        account_email = verify_personal_token(db, token)
        if account_email is None:
            click.echo(
                "authentication failed: DT_PERSONAL_TOKEN is invalid, "
                "revoked, or unknown", err=True)
            raise SystemExit(2)
        role = get_role(db, account_email)
        if role is None:
            # Token verified but account no longer exists (race: account
            # deleted between token creation and now).
            click.echo(
                "authentication failed: account no longer exists",
                err=True)
            raise SystemExit(2)
        return account_email, role

    if as_user is not None:
        password = os.environ.get("DT_USER_PASSWORD")
        if password is None:
            click.echo(
                "DT_USER_PASSWORD not set", err=True)
            raise SystemExit(2)
        from digital_twins.auth import authenticate
        ok = authenticate(db, as_user, password)
        if not ok:
            click.echo(f"authentication failed for '{as_user}'", err=True)
            raise SystemExit(2)
        role = get_role(db, as_user)
        if role is None:
            click.echo(
                "authentication failed: account no longer exists",
                err=True)
            raise SystemExit(2)
        return as_user, role

    click.echo(
        "authentication failed: DT_PERSONAL_TOKEN not set "
        "(export DT_PERSONAL_TOKEN=<token> to authenticate)",
        err=True)
    raise SystemExit(2)


@cli.group()
def token() -> None:
    """Manage personal tokens (create / list / revoke).

    All subcommands authenticate first via DT_PERSONAL_TOKEN, then check
    the caller's role against R3.
    """


@token.command("create")
@click.option("--as", "as_user", type=str, default=None,
              help="Create a token for this user (admin-only). "
                   "Without --as: the caller's own token.")
def token_create(as_user: str) -> None:
    """Create a personal token.

    Without ``--as``: the caller's own token (self-service; any
    authenticated role with manage_own_tokens). With ``--as USER``:
    admin-only, create for another user. Prints the plaintext token once.
    """
    from digital_twins.auth import create_personal_token
    from digital_twins.accounts import require_capability, RoleDenied

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _token_authenticate(db, as_user)

        if as_user is not None:
            # --as USER: admin-only
            try:
                require_capability(
                    caller_role, "manage_accounts",
                    "create a token for another user")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)
            target_email = as_user
            # Verify the target user exists
            from digital_twins.accounts import get_role as _get_role
            target_role = _get_role(db, target_email)
            if target_role is None:
                click.echo(
                    f"user `{target_email}` not found", err=True)
                raise SystemExit(2)
        else:
            # Self-service: the caller's own token
            target_email = caller_email
            try:
                require_capability(
                    caller_role, "manage_own_tokens",
                    "create a personal token")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)

        token_id, plaintext = create_personal_token(db, target_email)
        click.echo(plaintext)
        click.echo(f"token id={token_id} created for `{target_email}` "
                   f"(store it now — it will not be shown again)")
    finally:
        db.close()


@token.command("list")
@click.option("--as", "as_user", type=str, default=None,
              help="List tokens for this user (admin-only). "
                   "Without --as: the caller's own tokens.")
def token_list(as_user: str) -> None:
    """List personal tokens (metadata only; plaintext never re-displayed).

    Without ``--as``: self: own tokens; admin: all. With ``--as USER``:
    admin-only: list a specific user's tokens.
    """
    from digital_twins.auth import list_personal_tokens
    from digital_twins.accounts import require_capability, RoleDenied, get_role

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _token_authenticate(db, as_user)

        if as_user is not None:
            # --as USER: admin-only
            try:
                require_capability(
                    caller_role, "manage_accounts",
                    "list another user's tokens")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)
            target_role = get_role(db, as_user)
            if target_role is None:
                click.echo(f"user `{as_user}` not found", err=True)
                raise SystemExit(2)
            rows = list_personal_tokens(db, as_user)
        elif caller_role == "admin":
            # Admin without --as: all tokens
            rows = list_personal_tokens(db)
        else:
            # Non-admin without --as: own tokens only
            try:
                require_capability(
                    caller_role, "manage_own_tokens",
                    "list personal tokens")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)
            rows = list_personal_tokens(db, caller_email)

        if not rows:
            click.echo("no tokens")
            return

        # Print a table: id, account_email, created_at, last_used_at, revoked
        header = ("id", "account_email", "created_at", "last_used_at",
                  "revoked")
        col_keys = ("id", "account_email", "created_at", "last_used_at",
                    "revoked")
        widths = [
            max(len(h), *(len(str(r[c]) if r[c] is not None else "-")
                           for r in rows))
            for h, c in zip(header, col_keys)
        ]
        click.echo("  ".join(h.ljust(w) for h, w in zip(header, widths)))
        for r in rows:
            cells = [
                str(r[c]) if r[c] is not None else "-"
                for c in col_keys
            ]
            click.echo("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    finally:
        db.close()


@token.command("revoke")
@click.option("--id", "token_id", type=int, required=True,
              help="Token id to revoke.")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Requires DT_USER_PASSWORD.")
def token_revoke(token_id: int, as_user: str) -> None:
    """Revoke a personal token by id.

    Self: own tokens only; admin: any. Flips ``revoked=1`` on that row
    only (US3 S3: revocation isolation).
    """
    from digital_twins.auth import revoke_personal_token, list_personal_tokens
    from digital_twins.accounts import require_capability, RoleDenied

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _token_authenticate(db, as_user)

        # Check the token exists and determine its owner
        # Scope the query to the caller's tokens for non-admins to avoid
        # information leak (non-admins should not enumerate all token ids).
        filter_email = None if caller_role == "admin" else caller_email
        rows = list_personal_tokens(db, filter_email)
        target = None
        for r in rows:
            if r["id"] == token_id:
                target = r
                break
        if target is None:
            click.echo(
                f"token id {token_id} not found", err=True)
            raise SystemExit(1)

        target_email = target["account_email"]
        if target_email != caller_email:
            # Revoking someone else's token: admin-only
            try:
                require_capability(
                    caller_role, "manage_accounts",
                    "revoke another user's token")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)

        # Self: must have manage_own_tokens
        if target_email == caller_email:
            try:
                require_capability(
                    caller_role, "manage_own_tokens",
                    "revoke a personal token")
            except RoleDenied as exc:
                click.echo(str(exc), err=True)
                raise SystemExit(2)

        revoke_personal_token(db, token_id)
        click.echo(f"revoked token id={token_id} "
                   f"(account: `{target_email}`)")
    finally:
        db.close()


# --- session group (003 US3 S3: revocable web sessions)
#
# The sessions table (T002/T006) stores pbkdf2 hashes of 32-byte web session
# tokens with an 8-hour TTL and a revoked flag. US3 S3 requires revocation
# isolation: "revoking one does not affect the other."  A leaked session
# token must be revocable; otherwise its only mitigation is the 8-hour TTL.
#
# `session revoke` takes the plaintext session token and flips revoked=1.
# It authenticates the caller (DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as)
# and requires the manage_sessions capability would be ideal, but R3 has no
# such cap — v1 treats session revocation as an admin action (a user's
# session tokens belong to their account; only admin can revoke another
# user's session).  For own-session revocation, the caller authenticates as
# the session owner.


@cli.group()
def session() -> None:
    """Manage web sign-in sessions (revoke).

    A web session token is a 32-byte value issued at /signin, stored as a
    pbkdf2 hash with an 8-hour TTL. ``session revoke`` flips the
    ``revoked`` flag so the token stops authenticating immediately.
    """


@session.command("revoke")
@click.argument("token", envvar="DT_SESSION_TOKEN")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Defaults to the session owner.")
def session_revoke(token: str, as_user: str) -> None:
    """Revoke a web session token so it stops authenticating immediately.

    The token is the plaintext 32-byte value returned by /signin. It is read
    from the DT_SESSION_TOKEN env var (not argv, to avoid shell history).
    The caller must authenticate (DT_PERSONAL_TOKEN or DT_USER_PASSWORD +
    --as). Revoke succeeds for the session's own owner or an admin.
    """
    from digital_twins.auth import revoke_session
    from digital_twins.accounts import get_role, require_capability, RoleDenied

    db = _open_schedules_db()
    try:
        # Authenticate the caller first.
        caller_email, caller_role = _token_authenticate(db, as_user)

        # Resolve the session owner (the account the session belongs to).
        from digital_twins.auth import verify_session
        session_owner = verify_session(db, token)
        if session_owner is None:
            # No live, un-revoked, un-expired session matches this token.
            # Could be already revoked / expired / never created.
            click.echo(
                "no live session found for this token "
                "(expired, revoked, or unknown)", err=True)
            raise SystemExit(2)

        # Gate: the caller must be the session owner or an admin.
        if caller_email != session_owner:
            require_capability(caller_role, "manage_accounts",
                               "revoke another user's session")

        revoke_session(db, token)
        click.echo(f"revoked session for {session_owner}")
    except RoleDenied as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2)
    finally:
        db.close()


# --- account group (T011, US2 — account management, admin-gated except whoami)
#
# All four subcommands authenticate first via DT_PERSONAL_TOKEN, then check
# the caller's role against R3.
#
# Role checks (R3 / contracts/cli.md):
# - account list: admin-only (manage_accounts).
# - account set-role: admin-only (manage_accounts) + last-admin guard (SC-002).
# - account delete: admin-only (manage_accounts) + last-admin guard (SC-002).
# - account whoami: any authenticated role (the only non-admin-gated subcommand).


@cli.group()
def account() -> None:
    """Manage accounts (list / set-role / delete / whoami).

    All subcommands authenticate first via DT_PERSONAL_TOKEN.
    ``whoami`` is the only subcommand available to non-admins.
    """


def _account_authenticate(db, as_user: str = None) -> tuple[str, str]:
    """Authenticate the caller for account commands.

    Returns ``(account_email, role)`` on success. Exits 2 with a named
    reason on failure. Supports both DT_PERSONAL_TOKEN and
    DT_USER_PASSWORD + --as (via the shared token-group helper).
    """
    return _token_authenticate(db, as_user)


@account.command("list")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Requires DT_USER_PASSWORD.")
def account_list(as_user: str) -> None:
    """List all accounts: email, role, created_at, last_active (admin only)."""
    from digital_twins.accounts import require_capability, RoleDenied

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _account_authenticate(db, as_user)

        try:
            require_capability(
                caller_role, "manage_accounts",
                "list accounts")
        except RoleDenied as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)

        rows = db.execute(
            "SELECT email, role, created_at, last_active "
            "FROM accounts ORDER BY email"
        ).fetchall()
        if not rows:
            click.echo("no accounts")
            return

        header = ("email", "role", "created_at", "last_active")
        widths = [
            max(len(h), *(len(r[i] or "-") for r in rows))
            for i, h in enumerate(header)
        ]
        click.echo("  ".join(h.ljust(w) for h, w in zip(header, widths)))
        for r in rows:
            cells = [(r[i] or "-") for i in range(len(header))]
            click.echo("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    finally:
        db.close()


@account.command("set-role")
@click.option("--email", required=True,
              help="Account email address.")
@click.option("--role", type=click.Choice(["admin", "scheduler", "reader"]),
              required=True, help="New role.")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Requires DT_USER_PASSWORD.")
def account_set_role(email: str, role: str, as_user: str) -> None:
    """Change an account's role (admin only; last-admin guard SC-002)."""
    from digital_twins.accounts import (
        LastAdminError, get_role, require_capability, RoleDenied, set_role)

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _account_authenticate(db, as_user)

        try:
            require_capability(
                caller_role, "manage_accounts",
                "change a role")
        except RoleDenied as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)

        # Verify the target account exists
        target_role = get_role(db, email)
        if target_role is None:
            click.echo(f"account `{email}` not found", err=True)
            raise SystemExit(2)

        try:
            set_role(db, email, role)
        except LastAdminError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)

        # Record the change in the audit trail (constitution V)
        from digital_twins.state.models import start_audit_run, finish_audit_run
        import uuid
        run_id = f"set-role-{email}-{uuid.uuid4()}"
        start_audit_run(db, run_id, trigger="manual", scheduled_by=caller_email)
        finish_audit_run(db, run_id, "ok",
                         {"action": "set_role", "target": email,
                          "new_role": role})
        click.echo(f"set role for `{email}` to `{role}`")
    finally:
        db.close()


@account.command("delete")
@click.option("--email", required=True,
              help="Account email address to delete.")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Requires DT_USER_PASSWORD.")
def account_delete(email: str, as_user: str) -> None:
    """Delete an account (admin only; last-admin guard SC-002).

    Cascades to ``personal_tokens`` and ``sessions`` via FK
    ``ON DELETE CASCADE``.
    """
    from digital_twins.accounts import (
        LastAdminError, delete_account, get_role,
        require_capability, RoleDenied)

    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _account_authenticate(db, as_user)

        try:
            require_capability(
                caller_role, "manage_accounts",
                "delete an account")
        except RoleDenied as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)

        # Verify the target account exists
        target_role = get_role(db, email)
        if target_role is None:
            click.echo(f"account `{email}` not found", err=True)
            raise SystemExit(2)

        try:
            delete_account(db, email)
        except LastAdminError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)

        # Record the deletion in the audit trail (constitution V)
        from digital_twins.state.models import start_audit_run, finish_audit_run
        import uuid
        run_id = f"delete-{email}-{uuid.uuid4()}"
        start_audit_run(db, run_id, trigger="manual", scheduled_by=caller_email)
        finish_audit_run(db, run_id, "ok",
                         {"action": "delete", "target": email})
        click.echo(f"deleted account `{email}`")
    finally:
        db.close()


@account.command("whoami")
@click.option("--as", "as_user", default=None,
              help="Account to authenticate as. Requires DT_USER_PASSWORD.")
def account_whoami(as_user: str) -> None:
    """Print the caller's identity + role (authenticated, any role)."""
    cfg = load()
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_dir():
        click.echo(
            "no state db — run 'digital-twins init' first", err=True)
        raise SystemExit(2)
    db = connect(state_dir)
    try:
        migrate(db)
        caller_email, caller_role = _account_authenticate(db, as_user)
        click.echo(f"account: {caller_email}")
        click.echo(f"role: {caller_role}")
    finally:
        db.close()


# --- config group (T018, US3 — per-user overrides, R5) ----------------------
#
# All three subcommands (set, list, unset) authenticate first via
# DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as, then check the caller's
# role against R3 (manage_own_config for own user, manage_all_user_config
# for another user's).
#
# Key restriction: K is limited to R5's overridable knobs
# (enabled, max_items, timeout_s); other keys → exit 2
# ("not a user-overridable knob").
# Value type-coerced via 001 schema.coerce at write time (fail-fast).


def _config_authenticate(db, as_user: str) -> tuple[str, str]:
    """Authenticate the caller for config commands and resolve their role.

    Delegates to :func:`_token_authenticate` (the shared 003 auth helper).
    """
    return _token_authenticate(db, as_user)


def _config_check_target(
    db, caller_email: str, caller_role: str, target_email: str
) -> None:
    """Check the caller may manage config for ``target_email``.

    - Own user: any role with ``manage_own_config`` (admin/scheduler/reader).
    - Another user: admin-only (``manage_all_user_config``).
    Exits 2 with a named reason on denial.
    """
    from digital_twins.accounts import require_capability, RoleDenied

    if target_email == caller_email:
        # Own config: manage_own_config (all three roles)
        try:
            require_capability(caller_role, "manage_own_config",
                               "manage own personal config")
        except RoleDenied as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)
    else:
        # Another user's config: admin-only (manage_all_user_config)
        try:
            require_capability(caller_role, "manage_all_user_config",
                               "manage config for another user")
        except RoleDenied as exc:
            # The brief requires the message to read like:
            # "role `reader` may not manage config for another user"
            click.echo(
                f"role `{caller_role}` may not manage config "
                f"for another user", err=True)
            raise SystemExit(2)


@cli.group()
def config() -> None:
    """Manage per-user config overrides (R5).

    Subcommands: set, list, unset.
    All authenticate first (DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as),
    then check the caller's role against R3.
    """


@config.command("set")
@click.option("--as", "as_user", required=True,
              help="Account whose config to set. For own config: any "
                   "authenticated role. For another user's config: "
                   "admin-only.")
@click.option("--source", required=True,
              help="Source name (e.g. hermes, pi).")
@click.option("--key", required=True,
              help="Config key to set. Must be one of: enabled, max_items, "
                   "timeout_s.")
@click.option("--value", required=True,
              help="Value to set (type-coerced at write time).")
def config_set(as_user: str, source: str, key: str, value: str) -> None:
    """Set a per-user config override.

    The role must permit **manage own personal config** (admin/scheduler/reader
    for *their own* user; admin for another user's). ``K`` is restricted to
    the overridable keys; other keys → exit 2. ``V`` is type-coerced via
    001 ``schema.coerce`` at write time (fail-fast on a malformed value).
    """
    db = _open_schedules_db()
    try:
        caller_email, caller_role = _config_authenticate(db, as_user)
        _config_check_target(db, caller_email, caller_role, as_user)

        from digital_twins.user_config import set_override, NotUserOverridableError
        from digital_twins.config.schema import SchemaError

        try:
            set_override(db, as_user, source, key, value)
        except NotUserOverridableError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2)
        except SchemaError as exc:
            click.echo(f"invalid value for {key}: {exc}", err=True)
            raise SystemExit(2)

        click.echo(f"set {source}.{key}={value} for `{as_user}`")
    finally:
        db.close()


@config.command("list")
@click.option("--as", "as_user", required=True,
              help="Account whose overrides to list. Own: any authenticated "
                   "role. Another user's: admin-only.")
def config_list(as_user: str) -> None:
    """List a user's per-user config overrides."""
    db = _open_schedules_db()
    try:
        caller_email, caller_role = _config_authenticate(db, as_user)
        _config_check_target(db, caller_email, caller_role, as_user)

        from digital_twins.user_config import get_overrides
        overrides = get_overrides(db, as_user)

        if not overrides:
            click.echo("no overrides")
            return

        header = ("source", "key", "value")
        rows = [(src, key, val) for (src, key), val in
                sorted(overrides.items())]
        widths = [max(len(h), *(len(str(r[i]) if r[i] is not None else "-")
                                 for r in rows))
                  for i, h in enumerate(header)]
        click.echo("  ".join(h.ljust(w) for h, w in zip(header, widths)))
        for r in rows:
            cells = [str(c) if c is not None else "-" for c in r]
            click.echo("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    finally:
        db.close()


@config.command("unset")
@click.option("--as", "as_user", required=True,
              help="Account whose override to remove. Own: any authenticated "
                   "role. Another user's: admin-only.")
@click.option("--source", required=True,
              help="Source name (e.g. hermes, pi).")
@click.option("--key", required=True,
              help="Config key to remove. Must be one of: enabled, "
                   "max_items, timeout_s.")
def config_unset(as_user: str, source: str, key: str) -> None:
    """Remove a per-user config override."""
    db = _open_schedules_db()
    try:
        caller_email, caller_role = _config_authenticate(db, as_user)
        _config_check_target(db, caller_email, caller_role, as_user)

        from digital_twins.user_config import (
            OVERRIDABLE_KEYS,
            NotUserOverridableError,
            unset_override,
        )

        if key not in OVERRIDABLE_KEYS:
            click.echo(
                f"{key}: not a user-overridable knob "
                f"(overridable: {', '.join(sorted(OVERRIDABLE_KEYS))})",
                err=True)
            raise SystemExit(2)

        unset_override(db, as_user, source, key)

        click.echo(f"removed {source}.{key} for `{as_user}`")
    finally:
        db.close()


@cli.command("run-history")
@click.option("--as", "as_user", required=True,
              help="Account to view run history for. Own history: any "
                   "authenticated role. All users' history: admin-only.")
@click.option("--all", "all_users", is_flag=True,
              help="View all users' run history (admin-only).")
def run_history(as_user: str, all_users: bool) -> None:
    """View run history (audit records filtered to the caller's account).

    003 post-auth role check: the caller must have the ``view_own_history``
    capability (R3: all roles) to see their own runs, or
    ``view_all_history`` (R3: admin only) to see every user's runs with
    ``--all``.  ``system``-attributed runs are excluded from the "mine"
    view.

    Auth: DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as (shared 003 helper).
    """
    db = _open_schedules_db()
    try:
        from digital_twins.accounts import require_capability, RoleDenied
        caller_email, caller_role = _token_authenticate(db, as_user)
        from digital_twins.state.models import list_runs

        if all_users:
            require_capability(caller_role, "view_all_history",
                               "view all users' run history")
            runs = list_runs(db, user=caller_email, all_users=True)
        else:
            require_capability(caller_role, "view_own_history",
                               "view own run history")
            # "mine" = runs attributed to the authenticated caller, not
            # necessarily the --as target (you can only view your own).
            runs = list_runs(db, user=caller_email)

        if not runs:
            click.echo("no runs")
            return

        header = ("run_id", "started_at", "completed_at", "status",
                  "trigger", "scheduled_by")
        rows = [r[:6] for r in runs]
        widths = [max(len(h), *(len(str(r[i]) if r[i] is not None else "-")
                                 for r in rows))
                  for i, h in enumerate(header)]
        click.echo("  ".join(h.ljust(w) for h, w in zip(header, widths)))
        for r in rows:
            cells = [str(c) if c is not None else "-" for c in r]
            click.echo("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    except RoleDenied as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2)
    finally:
        db.close()


def main() -> None:
    cli()
