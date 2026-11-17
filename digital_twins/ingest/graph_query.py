"""Graph read path: Cypher queries over the S4 ``:SourceItem`` shape the
pipeline writes via :mod:`digital_twins.ingest.pipeline`
(openspec change s4-graph-alignment).

S4 graph shape (one node per source item, no chunk nodes):

    (:SourceItem {item_id, channel, content_hash})

The join key is the ``item_id`` property — the source's ``item.key``,
the same value written as the Qdrant payload ``item_id`` field — so a
retrieval flow can seed from Qdrant (vector hits on ``item_id``) and
expand into the graph for structurally-related content.

Chunk text does NOT live in the graph (S4 decision: Neo4j is the
entity/relation graph; Qdrant is the vector + payload store — chunk
content is in the Qdrant payload ``full_content``). Read functions
here therefore return item-level rows only.

All query functions accept a driver-shaped object exposing
``.session()`` returning a session that supports the context-manager
protocol and ``.run(query, **params)`` — the same minimal surface the
``neo4j`` package's ``GraphDatabase.driver(...)`` exposes, so these
functions work with a real driver or the recording test fakes in
``tests/conftest.py`` / ``tests/integration/test_neo4j_query.py``
without coupling to the concrete driver type.

This module is deliberately **additive**: it is only invoked when a
caller explicitly threads a Neo4j driver into the retrieval path.
Callers that leave ``neo4j`` at its default (``None``) skip every
function here entirely — the Qdrant-only retrieval path is unchanged.

025 FR-7 guard: all pinned Cypher in this module runs against the
session's database (the one the driver was built with, i.e.
``KB_NEO4J__URL``'s ``?db=`` target, or ``neo4j`` if unset). Both
queries are read-only; on a tenant install the driver URL MUST carry
the ``?db=`` pin or the reads silently fall through to the server
default.

The ``tenant_install`` keyword argument on :func:`expand_relatives`
and :func:`provenance` makes this guard *runtime*: when the caller
declares it is operating a tenant database, the function verifies the
driver URL carries the ``?db=`` pin and raises
:class:`~digital_twins.config.schema.ConfigError` if it does not
(plan D6, FR-7 guard).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# --- query templates ----------------------------------------------------------
# Pinned as module constants (not inline strings at call sites) so the
# retrieval Cypher has a single, testable source of truth — mirrors the
# write-side Cypher being pinned in tests/integration/test_neo4j_query.py.

# Neighbours: sibling SourceItems in the same channel (the "relatives"
# of a hit item, at item granularity — the S4 graph has no chunk nodes).
EXPAND_RELATIVES = (
    "MATCH (hit:SourceItem {item_id: $hit}) "
    "OPTIONAL MATCH (sib:SourceItem) "
    "WHERE sib.channel = hit.channel AND sib.item_id <> $hit "
    "RETURN sib.item_id AS item_id, sib.content_hash AS content_hash "
    "LIMIT $limit"
)

# Provenance: the SourceItem record for one item_id.
PROVENANCE = (
    "MATCH (si:SourceItem {item_id: $hit}) "
    "RETURN si.item_id AS item_id, si.channel AS channel, "
    "si.content_hash AS content_hash"
)


def expand_relatives(neo4j, item_id: str, limit: int = 5,
                     tenant_install: bool = False) -> list[dict]:
    """Return sibling ``SourceItem`` rows in the same channel as the
    given ``item_id`` (excluding the hit itself).

    ``item_id`` must be the join key — the Qdrant payload ``item_id``
    (= the source's ``item.key``).  Returns a list of
    ``{"item_id", "content_hash"}`` dicts, empty when there are no
    siblings or the graph has no entry for that ``item_id`` yet.

    *tenant_install*: when True, the FR-7 guard verifies the driver's
    URL carries a ``?db=`` pin and raises
    :class:`~digital_twins.config.schema.ConfigError` if it does not.
    """
    if tenant_install:
        _guard_db_pin(neo4j, "expand_relatives")
    with neo4j.session() as session:
        rows = session.run(EXPAND_RELATIVES, hit=item_id, limit=limit).data()
    return [dict(r) for r in rows]


def provenance(neo4j, item_id: str, tenant_install: bool = False) -> dict | None:
    """Return the ``SourceItem`` record for one ``item_id`` (or None).

    *tenant_install*: when True, the FR-7 guard verifies the driver's
    URL carries a ``?db=`` pin and raises
    :class:`~digital_twins.config.schema.ConfigError` if it does not.
    """
    if tenant_install:
        _guard_db_pin(neo4j, "provenance")
    with neo4j.session() as session:
        rows = session.run(PROVENANCE, hit=item_id).data()
    return dict(rows[0]) if rows else None


def _guard_db_pin(driver, caller: str) -> None:
    """FR-7 guard helper (mirrors ``neo4j_migration._guard_db_pin``).

    Verifies the driver's URL carries a ``?db=`` pin. Raises
    :class:`~digital_twins.config.schema.ConfigError` if the URL is
    missing or has no ``?db=`` query parameter.
    """
    from urllib.parse import urlparse, parse_qs
    from digital_twins.config.schema import ConfigError

    url: str | None = None
    addr = getattr(driver, "_address", None)
    if addr:
        if isinstance(addr, str):
            url = addr
        else:
            host, port = addr
            url = f"bolt://{host}:{port}"

    if not url:
        raise ConfigError(
            f"FR-7 guard ({caller}): driver URL not accessible; "
            f"cannot verify ?db= pin on a tenant install. Rebuild the "
            f"driver via build_neo4j_driver() which carries the pin "
            f"from config.")

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
