"""digital-tokens command-line interface."""

from pathlib import Path

import click

from digital_twins import __version__
from digital_twins.config import ConfigError, load
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


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


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Environment-portable KB ingestion: layered config, fail-fast sources, dedup-safe ingest."""
    pre_command()


def main() -> None:
    cli()
