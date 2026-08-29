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
            click.echo(
                f"authentication failed for '{as_user}': "
                f"no state db at {state_dir}", err=True)
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
    finally:
        conn.close()

    results = health.run_health_checks(load())
    _print_report(results)
    raise SystemExit(_exit_code(results))


def main() -> None:
    cli()
