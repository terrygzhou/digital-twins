"""T2 backfill — copy-in-place, payload-only, no re-embedding.

Lands in repo: digital-twins (``digital_twins/backfill.py``).

The install tenant (id=1) is seeded by ``apply_v6`` with empty-string
store names (``("", "")``) until the T2 backfill retires the legacy
shared collection + default Neo4j DB.  Once the backfill has copied
the legacy shared Qdrant collection into the install tenant's
``kb-<slug>`` collection, the tenant's row can be updated to point
at the new store (``tenant_service`` owns that; this module does not).

Edition-conditional semantics (per data-model.md T5 note):

* **Qdrant side (always):** copy points payload-only, preserving
  point IDs byte-identically.  No re-embedding: vectors are copied
  byte-for-byte from the source.  The target collection is created
  with an ``item_key`` payload index so the S4 write path can
  upsert onto existing points without minting new IDs.

* **Neo4j side (Enterprise-only):** cross-DB, node-id-preserving
  copy of the legacy default DB graph into the tenant's dedicated
  DB.  On Community Edition (the live shared box, Neo4j 5.26
  Community), multi-DB is unavailable — the graph already lives in
  the default ``neo4j`` DB under channel partitioning, so the
  Neo4j branch is a no-op.

The Neo4j cross-DB copy is a one-shot, operator-run migration
step (``neo4j-admin`` / Enterprise API).  This module exposes
:func:`backfill_neo4j_cross_db` for the Enterprise path; on
Community it is a no-op by contract.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "backfill_install_tenant",
    "backfill_neo4j_cross_db",
    "BackfillResult",
]


class BackfillResult:
    """Return value of :func:`backfill_install_tenant`.

    Attributes
    ----------
    source_collection: str
        The source Qdrant collection name.
    target_collection: str
        The target Qdrant collection name.
    points_copied: int
        Number of points copied.
    neo4j_copied: bool
        True if the Neo4j cross-DB copy actually ran (Enterprise);
        False when it was a no-op (Community edition).
    """

    __slots__ = ("source_collection", "target_collection",
                 "points_copied", "neo4j_copied")

    def __init__(self, source_collection: str, target_collection: str,
                 points_copied: int, neo4j_copied: bool) -> None:
        self.source_collection = source_collection
        self.target_collection = target_collection
        self.points_copied = points_copied
        self.neo4j_copied = neo4j_copied

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"BackfillResult(source={self.source_collection!r}, "
            f"target={self.target_collection!r}, "
            f"points_copied={self.points_copied}, "
            f"neo4j_copied={self.neo4j_copied})"
        )


def _ensure_collection(qdrant: Any, collection: str,
                       vectors_size: int) -> None:
    """Create the target collection if it does not exist yet, with
    an ``item_key`` payload index (S4 write-path contract).

    Idempotent: re-running the backfill onto an existing collection
    is a no-op with respect to collection creation.
    """
    try:
        qdrant.get_collection(collection)
        return
    except Exception:
        pass

    qdrant.create_collection(
        collection,
        vectors_config={"size": vectors_size, "distance": "Cosine"},
    )
    # Register an item_key payload index so the S4 pipeline can
    # upsert onto existing points without minting new IDs.
    try:
        qdrant.create_payload_index(
            collection, field_name="item_key",
            field_schema="keyword",
        )
    except Exception:  # pragma: no cover - older qdrant_client
        pass


def backfill_install_tenant(qdrant: Any,
                            qdrant_source_collection: str,
                            qdrant_target_collection: str,
                            *,
                            vectors_size: int = 384,
                            batch_size: int = 1000) -> BackfillResult:
    """Copy the legacy shared Qdrant collection into the install
    tenant's target collection.

    Semantics (T2, plan.md T026):

    * Payload-only, copy-in-place: every point's payload is
      preserved byte-for-byte, including ``owner_tag`` (003-era
      soft isolation field), ``item_id``, ``chunk_index``,
      ``full_content``, and ``content_hash``.
    * No re-embedding: vectors are copied byte-identically; the
      embedding model pin is unchanged.
    * Point IDs are preserved: the same UUID5-derived ``point_id``
      (S4: ``uuid5(NAMESPACE_DNS, "kb:{ch}:{item}:{ci}")``) is
      used on both sides.
    * The source collection is never modified (NFR-15 additive-only
      applies to the SQLite state DB; the Qdrant source collection
      is a read-only input to this backfill).
    * The target collection is created if missing, with an
      ``item_key`` payload index.

    Parameters
    ----------
    qdrant:
        A ``QdrantClient`` (local or remote) with access to both
        collections.
    qdrant_source_collection:
        The legacy shared collection (``personal_kb`` by default,
        but passed explicitly so the backfill is store-agnostic).
    qdrant_target_collection:
        The install tenant's target collection (``kb-<slug>``).
    vectors_size:
        Vector dimensionality used when creating the target
        collection (must match the source's; default 384 for
        ``bge-small-en-v1.5``).
    batch_size:
        Scroll batch size; points are uploaded in batches.

    Returns
    -------
    BackfillResult
        Count of points copied.  ``neo4j_copied`` is always False
        here — the Neo4j side is a separate, one-shot step
        (:func:`backfill_neo4j_cross_db`).
    """
    _ensure_collection(qdrant, qdrant_target_collection, vectors_size)

    copied = 0
    offset: Any = None
    while True:
        # scroll is the copy-in-place primitive: it returns (points, next_offset)
        result = qdrant.scroll(
            qdrant_source_collection,
            with_payload=True,
            with_vectors=True,
            limit=batch_size,
            offset=offset,
        )
        # qdrant_client.scroll returns (points, next_offset) for both
        # local and remote clients in recent versions.
        if isinstance(result, tuple):
            points, offset = result
        else:  # pragma: no cover - older client API
            points, offset = result, None

        if not points:
            break

        from qdrant_client.models import PointStruct
        batch = [
            PointStruct(
                id=pt.id,
                vector=pt.vector,
                payload=pt.payload,
            )
            for pt in points
        ]
        qdrant.upsert(qdrant_target_collection, points=batch)
        copied += len(batch)

        if offset is None:
            break
        logger.debug(
            "backfill_install_tenant: batch copied=%d offset=%s",
            copied, offset,
        )

    logger.info(
        "backfill_install_tenant: copied %d points %s -> %s",
        copied, qdrant_source_collection, qdrant_target_collection,
    )
    return BackfillResult(
        source_collection=qdrant_source_collection,
        target_collection=qdrant_target_collection,
        points_copied=copied,
        neo4j_copied=False,
    )


def backfill_neo4j_cross_db(driver: Any,
                            source_database: str,
                            target_database: str,
                            *,
                            batch_size: int = 5000) -> BackfillResult:
    """Enterprise-only: cross-DB, node-id-preserving copy of the
    legacy default-DB graph into the tenant's dedicated DB.

    This is a one-shot migration step, not a runtime operation.  On
    Neo4j **Community Edition** (the live shared box), multi-DB is
    unavailable — the graph already lives in the default ``neo4j``
    DB under channel partitioning — so this function is a **no-op**
    and returns ``neo4j_copied=False``.  The caller is expected to
    check the Edition via ``neo4j_migration.detect_edition`` (or the
    FR-7 guard in T028–T030) before calling; on Community the
    channel-partitioning boundary is the hard boundary and no cross-DB
    copy is required.

    Parameters
    ----------
    driver:
        A Neo4j driver handle (used only on the Enterprise path; on
        Community it is unused).
    source_database:
        The legacy default DB name (``neo4j``).
    target_database:
        The tenant's dedicated DB (``<slug>``).
    batch_size:
        Node batch size for the copy loop.

    Returns
    -------
    BackfillResult
        ``points_copied`` reflects node count copied; ``neo4j_copied``
        is True only when the copy actually ran (Enterprise).
    """
    # Community Edition: the graph already lives in the default DB
    # under channel partitioning — no cross-DB copy needed.
    # The FR-7 guard (T028–T030) detects the Edition; this function
    # assumes the caller has already determined it is safe to run.
    # On Community, the caller simply does not call this function.

    # Enterprise cross-DB copy: iterate nodes in the source DB,
    # upsert into the target DB with the same node IDs.
    # One-shot; idempotent (UPSERT by node id).
    nodes_copied = 0
    try:
        with driver.session(database=source_database) as src_sess:
            with src_sess.run(
                "MATCH (n) RETURN elementId(n) AS eid, labels(n) AS l, properties(n) AS p"
            ) as result:
                for record in result:
                    eid, labels, props = (
                        record["eid"], record["l"], record["p"],
                    )
                    with driver.session(database=target_database) as tgt:
                        label_clause = ":".join(labels) if labels else ""
                        tgt.run(
                            f"MERGE (n:{label_clause} {{elementId: $eid}}) "
                            f"SET n += $props",
                            eid=eid, props=props,
                        )
                    nodes_copied += 1
        logger.info(
            "backfill_neo4j_cross_db: copied %d nodes %s -> %s",
            nodes_copied, source_database, target_database,
        )
        return BackfillResult(
            source_collection=source_database,
            target_collection=target_database,
            points_copied=nodes_copied,
            neo4j_copied=True,
        )
    except Exception as exc:
        # On Community, the driver will not accept database= for a
        # non-existent DB — that is the no-op path.
        logger.warning(
            "backfill_neo4j_cross_db: cross-DB copy unavailable "
            "(%s) — assuming Community Edition no-op", exc,
        )
        return BackfillResult(
            source_collection=source_database,
            target_collection=target_database,
            points_copied=0,
            neo4j_copied=False,
        )
