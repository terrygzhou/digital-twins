"""S4 graph migration (openspec s4-graph-alignment, task 4.1).

One-shot cleanup of the pre-S4 legacy graph labels. The S4 write path
(``ingest.pipeline._upsert_graph``) writes ``:SourceItem`` nodes only; the
pre-S4 pipeline wrote ``:KbItem``/``:KbChunk`` with ``HAS_CHUNK`` edges,
and those nodes carry no data the S4 shape reads — the design decision
(spec Non-goals) is delete + re-ingest, not in-place translation.

``migrate_s4`` is idempotent and a no-op on a database that never held
the legacy labels: it reports 0 nodes deleted and exits cleanly.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_LEGACY_LABELS = ("KbItem", "KbChunk")


def migrate_s4(driver: Any, dry_run: bool = False,
               tenant_install: bool = False) -> dict:
    """Remove legacy ``:KbItem`` / ``:KbChunk`` nodes.

    *dry_run*: run count queries only — no mutation — and report the
    node counts that a real run would delete.

    *tenant_install*: when True, the caller asserts this is a 025
    multi-tenant install. The FR-7 guard then verifies that the
    driver's URL carries a ``?db=`` pin; if the URL is missing the
    pin, a :class:`~digital_twins.config.schema.ConfigError` is
    raised (fail-fast, consistent with
    :func:`digital_twins.scheduler.loop.build_neo4j_driver` on
    Enterprise).

    The driver contract is the pipeline hand-off surface (002 US2)
    after graph-driver-fix (BUG-01): the session API — ``with
    driver.session() as s: s.run(query, **params)``, whose result
    carries ``.single()``. The caller hands over a raw
    ``neo4j.GraphDatabase.driver``
    (``scheduler.loop.build_neo4j_driver``), which exposes no
    driver-level ``.run()``.
    ``:SourceItem`` nodes are never touched — the S4 write path owns
    them; a fresh database therefore migrates to {0, 0}.

    025 FR-7 guard: all pinned Cypher in this module runs against the
    session's database (the one the driver was built with, i.e.
    ``KB_NEO4J__URL``'s ``?db=`` target, or ``neo4j`` if unset). A
    destructive legacy-label cleanup therefore runs inside the
    connected database — pin the URL explicitly when operating a
    tenant DB.

    The ``tenant_install`` parameter makes this guard *runtime*, not
    just a docstring note: a caller operating a tenant database must
    set it to True, and the function will fail fast if the driver
    was not built with the ``?db=`` pin. This mirrors the
    ``build_neo4j_driver`` contract (plan D6).
    """
    result: dict = {"dry_run": dry_run}

    # FR-7 runtime guard: when the caller declares this is a tenant
    # install, verify the driver URL carries a ?db= pin.
    if tenant_install:
        _guard_db_pin(driver, "migrate_s4")

    counts: dict[str, int] = {}
    with driver.session() as s:
        for label in _LEGACY_LABELS:
            q = f"MATCH (n:{label}) "
            counts[label] = s.run(q + "RETURN count(n) AS n").single()["n"]
        if not dry_run:
            for label in _LEGACY_LABELS:
                s.run(f"MATCH (n:{label}) DETACH DELETE n")
    if dry_run:
        result["would_delete"] = counts
    else:
        result["deleted"] = counts
    return result


def _guard_db_pin(driver: Any, caller: str) -> None:
    """FR-7 guard helper: verify the driver's URL carries a ``?db=`` pin.

    Extracts the URL from the driver's constructor arguments (the
    ``neo4j`` driver stores it as ``driver._address`` or in the
    constructor's recorded args).  If no URL is accessible, or the URL
    is missing a ``?db=`` query parameter, a
    :class:`~digital_twins.config.schema.ConfigError` is raised.

    This mirrors :func:`digital_twins.scheduler.loop.build_neo4j_driver`'s
    Enterprise guard (plan D6).
    """
    from urllib.parse import urlparse, parse_qs

    from digital_twins.config.schema import ConfigError

    # neo4j driver instances expose the URL in several ways depending
    # on the version: ``driver._address`` (older), or the recorded
    # constructor arg.  Try both.
    url: str | None = None
    addr = getattr(driver, "_address", None)
    if addr:
        # _address is a (host, port) tuple on some versions; if it's
        # a string, use it directly.
        if isinstance(addr, str):
            url = addr
        else:
            # Reconstruct a plausible URL from (host, port).
            host, port = addr
            url = f"bolt://{host}:{port}"
    else:
        # Fall back to the driver's ``_config`` if present.
        config = getattr(driver, "_config", None)
        if config is not None:
            # Some neo4j versions store the address in _config.
            import dataclasses
            if dataclasses.is_dataclass(config):
                fields = {f.name: getattr(config, f.name)
                          for f in dataclasses.fields(config)}
                host = fields.get("address", ("localhost", 7687))
                if isinstance(host, tuple):
                    host, port = host
                    url = f"bolt://{host}:{port}"
                else:
                    url = str(host)

    if not url:
        logger.warning(
            "%s: could not extract driver URL for FR-7 pin check; "
            "assuming un-pinned — raise ConfigError only on explicit "
            "tenant_install=True", caller,
        )
        # We cannot verify, so we let the caller decide. If
        # tenant_install=True, we must still guard.
        if caller != "migrate_s4":
            return
        # On the tenant_install path we are being strict:
        raise ConfigError(
            "FR-7 guard: driver URL not accessible; cannot verify "
            "?db= pin on a tenant install. Rebuild the driver via "
            "build_neo4j_driver() which carries the pin from config.")

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if not qs.get("db"):
        raise ConfigError(
            f"FR-7 guard ({caller}): driver URL {url!r} has no ?db= "
            f"query parameter. On a tenant install, KB_NEO4J__URL "
            f"must carry the ?db=<tenant> pin, or the query will "
            f"silently fall through to the server default. Rebuild "
            f"the driver via build_neo4j_driver() which carries the "
            f"pin from config.")
