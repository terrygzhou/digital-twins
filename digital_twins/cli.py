"""digital-twins command-line interface."""

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
        line = f"{r.endpoint:<9} {'ok' if r.ok else 'FAIL':<5} {r.detail}"
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
        finally:
            db.close()
        if not ok:
            click.echo(f"authentication failed for '{as_user}'", err=True)
            raise SystemExit(2)
        scheduled_by = as_user
    else:
        scheduled_by = "system"

    # --once: one-shot host-cron run. trigger='manual', scheduled_by per
    # --as (or 'system'), no schedule advance, no pidfile. Without --once
    # the 001 behavior is unchanged. (T008's `serve` will use
    # trigger='schedule'.)
    trigger = "manual"
    cfg = load()
    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)

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
                trigger=trigger, scheduled_by=scheduled_by)
        except PrerequisiteError as exc:
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
    from digital_twins.ingest.embedding import load_embedder

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

    No endpoint-health gate at startup: 001's ``run`` does not gate on
    endpoint health — it gates on per-source prerequisites, and the
    pipeline fails per-source when an endpoint is actually needed.
    ``serve`` matches that: a down endpoint at startup is not a reason to
    refuse to start (an LLM-only pipeline can run with qdrant down). Per-
    source failures at fire time are reported, never silent — T006's
    ``serve_once_tick`` audits a ``failed`` row and advances (R-07).

    --port overrides scheduler.status_port; --port 0 disables the status
    endpoint entirely (no socket bound). Fails fast with exit code 2 if
    another live serve holds the pidfile (run_serve raises that; the CLI
    maps it to exit 2). SIGTERM/SIGINT -> clean shutdown (handled by
    run_serve).
    """
    cfg = load()

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
                status_server = StatusServer(
                    ("127.0.0.1", status_port), db, cfg)
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


# --- schedule group (T012, US3 — FR-3 v1 CRUD) -----------------------------
#
# Ruling R-12: ``--as`` on schedule commands is an OWNER LABEL, not an auth
# requirement. No DT_USER_PASSWORD, no accounts check, no credential
# prompt. Auth via DT_USER_PASSWORD is T011 and lives on ``run --once``
# only.


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
@click.option("--as", "as_user", default="system", show_default=True,
              help="Owner label (R-12: a label, not an auth check).")
def schedule_add(source: str, preset: str, param: int, fire_time: str,
                 as_user: str) -> None:
    """Add a schedule (upserts on the 5-field key)."""
    import sqlite3 as _sqlite3
    db = _open_schedules_db()
    try:
        from digital_twins.scheduler.schedules import create_schedule
        try:
            row = create_schedule(db, as_user, source, preset,
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
    """List schedules. ``--as`` filters by owner label (R-12)."""
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
def schedule_remove(schedule_id: int) -> None:
    """Remove a schedule by id. Exit 1 if the id does not exist."""
    db = _open_schedules_db()
    try:
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


def _token_authenticate(db) -> tuple[str, str]:
    """Authenticate the caller via DT_PERSONAL_TOKEN.

    Returns ``(account_email, role)`` on success. Exits 2 with a named
    reason on failure.
    """
    from digital_twins.auth import verify_personal_token
    from digital_twins.accounts import get_role

    token = os.environ.get("DT_PERSONAL_TOKEN")
    if not token:
        click.echo(
            "authentication failed: DT_PERSONAL_TOKEN not set "
            "(export DT_PERSONAL_TOKEN=<token> to authenticate)",
            err=True)
        raise SystemExit(2)

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
        caller_email, caller_role = _token_authenticate(db)

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
        caller_email, caller_role = _token_authenticate(db)

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
def token_revoke(token_id: int) -> None:
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
        caller_email, caller_role = _token_authenticate(db)

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


def main() -> None:
    cli()
