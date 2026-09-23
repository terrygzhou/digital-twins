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
"""

from __future__ import annotations

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


def expand_relatives(neo4j, item_id: str, limit: int = 5) -> list[dict]:
    """Return sibling ``SourceItem`` rows in the same channel as the
    given ``item_id`` (excluding the hit itself).

    ``item_id`` must be the join key — the Qdrant payload ``item_id``
    (= the source's ``item.key``).  Returns a list of
    ``{"item_id", "content_hash"}`` dicts, empty when there are no
    siblings or the graph has no entry for that ``item_id`` yet.
    """
    with neo4j.session() as session:
        rows = session.run(EXPAND_RELATIVES, hit=item_id, limit=limit).data()
    return [dict(r) for r in rows]


def provenance(neo4j, item_id: str) -> dict | None:
    """Return the ``SourceItem`` record for one ``item_id`` (or None)."""
    with neo4j.session() as session:
        rows = session.run(PROVENANCE, hit=item_id).data()
    return dict(rows[0]) if rows else None
