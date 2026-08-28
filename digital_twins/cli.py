"""digital-twins command-line interface."""

import click

from digital_twins import __version__


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Environment-portable KB ingestion: layered config, fail-fast sources, dedup-safe ingest."""


def main() -> None:
    cli()
