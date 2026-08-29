"""digital-twins command-line interface."""

from pathlib import Path

import click
import yaml

from digital_twins import __version__, health
from digital_twins.config import ConfigError, load
from digital_twins.config.loader import _merge
from digital_twins.config.schema import BUILTIN_SOURCES, get
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

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


def _starter(overrides: dict, cfg: dict) -> dict:
    """Starter kb.local.yml: resolved endpoints + every built-in source disabled."""
    data: dict = {
        "sources": {
            name: {"enabled": False, "max_items": 200, "timeout_s": 1500}
            for name in BUILTIN_SOURCES
        }
    }
    for prefix, keys in _ENDPOINT_SECTIONS:
        values = {}
        for key in keys:
            value = overrides.get(f"{prefix}.{key}") or get(cfg, f"{prefix}.{key}")
            if value:
                values[key] = value
        if values:
            data[prefix] = values
    return data


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Environment-portable KB ingestion: layered config, fail-fast sources, dedup-safe ingest."""
    pre_command()


@cli.command()
def validate() -> None:
    """Health-check the configured endpoints. Exits 0 only when all pass."""
    results = health.run_health_checks(load())
    _print_report(results)
    raise SystemExit(_exit_code(results))


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
