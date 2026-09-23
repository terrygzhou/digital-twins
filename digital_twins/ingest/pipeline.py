"""Ingestion pipeline: read -> chunk -> embed -> upsert -> high-water -> audit.

Fail-fast (cli.md): every enabled source is prerequisite-checked BEFORE any
endpoint is touched; a missing prerequisite raises PrerequisiteError (the CLI
maps it to exit 2) and the run is audited as `failed`.

Dedup (NFR-1): point IDs are deterministic (prefix|item_key|chunk|hash), so
re-reading the same content upserts the same points instead of duplicating
them. High-water marks additionally skip unchanged items.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from qdrant_client import models as qm

from digital_twins.config.schema import get
from digital_twins.health import QDRANT_COLLECTION, preflight
from digital_twins.ingest.chunking import chunk_text
from digital_twins.ingest.embedding import (
    DEFAULT_MODEL,
    build_endpoint_embedder,
    load_embedder,
    model_dimension,
)
from digital_twins.ingest.ids import content_hash, point_id_s4
from digital_twins.sources import UnknownSourceError, build as build_source
from digital_twins.state.models import (
    finish_audit_run,
    start_audit_run,
    upsert_highwater,
)
from digital_twins.ingest import entities  # s4-entity-extraction


class PrerequisiteError(Exception):
    """An enabled source is missing prerequisites; nothing is ingested."""

    def __init__(self, source: str, missing: list):
        self.source = source
        self.missing = missing
        super().__init__(
            f"source '{source}': missing prerequisite(s): " + "; ".join(missing))


class DimensionMismatchError(Exception):
    """Qdrant collection dimension != pinned model dimension (S2-mismatch, NFR-2)."""

    def __init__(self, collection: str, actual: int, expected: int):
        self.collection = collection
        self.actual = actual
        self.expected = expected
        super().__init__(
            f"collection '{collection}' is {actual}-dim but the pinned model "
            f"produces {expected}-dim vectors — "
            f"recreate the collection at the pinned dimension or re-embed (FR-010)")


@dataclass
class RunSummary:
    run_id: str
    counts: dict = field(default_factory=dict)
    points: int = 0
    status: str = "ok"


def _resolve_embedder(cfg):
    """Resolve the embedder from config: endpoint-based when
    ``embedding.endpoint`` is set, in-process pinned model otherwise.

    Shared by the CLI / scheduler / MCP entry points so every ingestion
    path honors ``embedding.endpoint`` (US2 / FR-003).
    """
    if get(cfg, "embedding.endpoint"):
        return build_endpoint_embedder(cfg)
    return load_embedder(
        get(cfg, "embedding.model"),
        get(cfg, "embedding.device") or "auto",
    )


def _cursor(db, source: str):
    row = db.execute(
        "SELECT MAX(last_key) FROM highwater WHERE source=?", (source,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _ensure_collection(client, dim: int) -> None:
    """Create the collection at `dim` if missing.

    The dimension guard (verify existing collection == pinned dim, raise
    DimensionMismatchError) is the caller's responsibility: see
    `assert_dimension` below. `_ensure_collection` only handles the
    not-yet-created case so callers that pre-check can stay minimal.
    """
    if not client.collection_exists(QDRANT_COLLECTION):
        client.create_collection(
            QDRANT_COLLECTION,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )


def assert_dimension(client, dim: int) -> None:
    """Guard: existing collection dimension must equal the pinned `dim`.

    Raises DimensionMismatchError (naming both dimensions + remediation)
    if the collection exists at a different size. No-op when the
    collection does not exist yet (it is created at `dim` on first run).
    """
    if not client.collection_exists(QDRANT_COLLECTION):
        return
    info = client.get_collection(QDRANT_COLLECTION)
    vectors = info.config.params.vectors
    if hasattr(vectors, "size"):  # single-vector collection
        actual = vectors.size
    else:
        first = next(iter(vectors.values()), None)
        actual = first.size if first is not None else None
    if actual != dim:
        raise DimensionMismatchError(QDRANT_COLLECTION, actual, dim)


def run_pipeline(
    cfg: dict,
    db,
    qdrant,
    embedder=None,
    neo4j=None,
    source_names: list | None = None,
    max_items: int | None = None,
    dry_run: bool = False,
    trigger: str = "manual",
    scheduled_by: str = "system",
    owner: str | None = None,
) -> RunSummary:
    """Run one ingestion pass over the enabled (or named) sources.

    `qdrant` is a client or a zero-arg factory (resolved only after the
    prerequisite check passes). `embedder` maps list[str] -> list of vectors
    and must be lazy about its own heavy setup. `neo4j` is a driver-shaped
    object exposing `.run(query, **params)` (None = skip graph writes).

    `trigger` records how the run was started ('manual' for run --once / the
    CLI one-shot, 'schedule' for serve fires). `scheduled_by` records the
    owning user ('system' when run --once has no --as; T011 sets the owner
    user later).

    `owner` (R6/003): when set, each point's Qdrant payload gains
    `"owner": owner` and `"owner_tag": f"{owner}-ingest"` (the query-time
    filter for per-user scoping, SC-005). When `None` (the default), the
    payload is unchanged (001/002 behavior). The owner tag is a payload
    field only — it does NOT enter the deterministic point ID, so the
    one-record dedup invariant (NFR-1) is preserved: the same content
    ingested by two different owners still yields one point.
    """
    if not dry_run:
        # 008 US1 gate: hard dependencies, before any write. dry-run skips
        # the gate: it consumes no service capacity and writes nothing
        # (diff-review P2 — a preview must not fail-fast on live services).
        preflight(cfg)
    run_id = str(uuid.uuid4())
    built: list = []
    counts: dict = {}
    start_audit_run(db, run_id, trigger=trigger, scheduled_by=scheduled_by)
    try:
        if source_names is None:
            source_names = [n for n, e in cfg["sources"].items()
                            if e.get("enabled")]
        # fail-fast: check every enabled source before touching anything
        for name in source_names:
            entry = cfg["sources"].get(name)
            if entry is None:
                raise UnknownSourceError(name)
            source = build_source(name, entry)
            missing = source.prerequisites()
            if missing:
                for s in built:
                    s[1].close()
                raise PrerequisiteError(name, missing)
            built.append((name, source))

        max_chars = get(cfg, "chunking.max_chars")
        overlap = get(cfg, "chunking.overlap")
        client = None
        if not dry_run:
            client = qdrant() if callable(qdrant) and not hasattr(qdrant, "upsert") else qdrant
            expected_dim = model_dimension(
                get(cfg, "embedding.model") or DEFAULT_MODEL)
            assert_dimension(client, expected_dim)
            _ensure_collection(client, expected_dim)

        total_points = 0
        for name, source in built:
            entry = cfg["sources"][name]
            cap_max = entry.get("max_items")
            since = _cursor(db, name)
            items = []
            for item in source.read(since):
                items.append(item)
                if (max_items and len(items) >= max_items) or \
                   (cap_max and len(items) >= cap_max):
                    break
            counts[name] = len(items)

            # S4 point-ID scheme (s4-graph-alignment): content-independent
            # (channel, item_id, chunk_index) identity, shared with
            # personal-kb's one uuid5 ID space. Chunk count is known before
            # embedding, so it can be stamped on every chunk's payload.
            chunks = []  # (point_id, chunk_index, item, text, n_chunks)
            for item in items:
                texts = chunk_text(item.content, max_chars, overlap)
                for i, text in enumerate(texts):
                    pid = point_id_s4(name, item.key, i)
                    chunks.append((pid, i, item, text, len(texts)))

            if dry_run:
                total_points += len(chunks)
                continue

            if chunks:
                vectors = embedder([text for *_x, text in chunks])
                # Item-level content hash (S4): shared by every chunk of
                # one item — the staleness signal that replaces the
                # legacy content-dependent point ID.
                item_hash = {item.key: content_hash(item.content)
                             for item in items}
                embed_model = get(cfg, "embedding.model") or DEFAULT_MODEL
                points = []
                for (pid, i, item, text, n), vector in zip(chunks, vectors):
                    payload = {
                        "source": name,
                        "source_url": f"{source.capability.prefix}{item.key}",
                        "item_key": item.key,
                        "chunk_index": i,
                        "ts": item.ts,
                        "text": text,
                        # --- S4 payload alignment (s4-graph-alignment) ---
                        # item_id: the join key into SourceItem.item_id.
                        "item_id": item.key,
                        # item-level hash, shared by all chunks of this item.
                        "content_hash": item_hash[item.key],
                        "full_content": text,
                        "content_snippet": text[:200],
                        "captured_at": item.ts,
                        "total_chunks": n,
                        "embed_model": embed_model,
                        "source_type": name,
                        "source_title": item.metadata.get("title", "") if hasattr(item, "metadata") else "",
                        "tags": item.metadata.get("tags", []) if hasattr(item, "metadata") else [],
                        # 003 multi-user: owner stamping moved under `meta`
                        # (query-time filter for per-user scoping, SC-005).
                        # The owner tag is a payload field only — it does NOT
                        # enter the point ID (NFR-1).
                        "meta": {
                            "owner": owner,
                            "owner_tag": f"{owner}-ingest" if owner else None,
                        },
                    }
                    # Optional provenance passthroughs (S4 decision): when
                    # written they equal the audit row's values for this run
                    # verbatim — no new value vocabulary.
                    payload["run_id"] = run_id
                    payload["trigger"] = trigger
                    points.append(qm.PointStruct(
                        id=pid,
                        vector=vector,
                        payload=payload,
                    ))
                client.upsert(QDRANT_COLLECTION, points=points, wait=True)
                total_points += len(points)
                if neo4j is not None:
                    # S4 graph write: one :SourceItem node per item, keyed
                    # on the join key (item.key). No chunk nodes, no chunk
                    # text in the graph (chunk text lives in Qdrant
                    # full_content).
                    _upsert_graph(neo4j, name, items, item_hash)
                    # Post-graph entity extraction (s4-entity-extraction,
                    # config-gated; no-op when extraction.enabled is false
                    # or the LLM endpoint is unconfigured).
                    _run_extraction(cfg, neo4j, items, item_hash, run_id, name)

            for item in items:
                upsert_highwater(db, name, item.key, item.ts)

        status = "ok"
        finish_audit_run(db, run_id, status, counts)
        return RunSummary(run_id, counts, total_points, status)
    except Exception:
        finish_audit_run(db, run_id, "failed", counts)
        raise
    finally:
        for _, source in built:
            source.close()


def _upsert_graph(neo4j, channel: str, items, item_hash: dict) -> None:
    """S4 graph write: one ``:SourceItem`` node per item, deduped on the
    join key ``item_id`` (= the source's ``item.key`` — the same value
    written as the Qdrant payload ``item_id``).

    ``MERGE`` makes the write idempotent: re-ingesting the same item
    re-stamps ``channel``/``content_hash`` in place instead of minting
    a node. The node carries no chunk text — the chunk content lives in
    Qdrant's ``full_content`` payload field (S4 decision: Neo4j is the
    entity/relation graph; Qdrant is the vector + payload store).
    ``content_hash`` is the item-level hash (shared by all chunks),
    computed by the caller and passed in.
    """
    _ensure_graph_schema(neo4j)
    for item in items:
        neo4j.run(
            "MERGE (si:SourceItem {item_id: $id}) "
            "SET si.channel = $ch, si.content_hash = $hash",
            id=item.key, ch=channel,
            hash=item_hash[item.key])


def _ensure_graph_schema(neo4j) -> None:
    """Idempotent schema bootstrap for the S4 graph.

    Secondary index on ``SourceItem.item_id`` (the join key into the
    Qdrant payload ``item_id``) and on ``SourceItem.channel`` (the
    provenance value used by channel-scoped graph reads).
    ``CREATE INDEX IF NOT EXISTS`` is a no-op on an already-created
    index, so this is safe to run on every graph-enabled pipeline pass.
    """
    neo4j.run(
        "CREATE INDEX IF NOT EXISTS FOR (si:SourceItem) ON (si.item_id)")
    neo4j.run(
        "CREATE INDEX IF NOT EXISTS FOR (si:SourceItem) ON (si.channel)")


def _run_extraction(
    cfg: dict,
    neo4j,
    items: list,
    item_hash: dict,
    run_id: str,
    channel: str,
) -> None:
    """Post-graph LLM extraction step (s4-entity-extraction).

    Config-gated: when ``extraction.enabled`` is false (the default) or
    ``neo4j`` is None, this is a no-op. When enabled and a driver is
    present, for each ingested item:

    1. ``entities.supersede(item.key, driver=neo4j, cfg=extraction_cfg)``
       — stamps ``valid_to`` on live MENTIONED/REL edges (BR-6.4).
    2. ``entities.extract(item.content, title=..., cfg=extraction_cfg)``
       — one LLM call per item (isolated: failures are logged, not raised).
    3. ``entities.materialize(...)`` — idempotent MERGE into the graph.

    Per-item failure isolation: an item whose extraction fails logs a
    warning and the remaining items proceed (matching personal-kb's
    ``post_sweep`` isolation pattern).
    """
    import logging

    extraction_cfg = {
        "enabled": bool(get(cfg, "extraction.enabled")),
        "max_text_chars": get(cfg, "extraction.max_text_chars") or 12000,
        "prompt_version": get(cfg, "extraction.prompt_version") or "",
    }
    if not extraction_cfg["enabled"]:
        return
    if neo4j is None:
        logging.debug(
            "extraction enabled but no Neo4j driver — skipping graph "
            "entity extraction")
        return

    llm_cfg = {
        "endpoint": get(cfg, "llm.endpoint"),
        "model": get(cfg, "llm.model"),
        "api_key": get(cfg, "llm.api_key"),
    }
    if not llm_cfg["endpoint"]:
        logging.debug(
            "extraction enabled but llm.endpoint not configured — "
            "skipping entity extraction")
        return

    for item in items:
        try:
            entities.supersede(item.key, driver=neo4j, cfg=extraction_cfg)
            extraction = entities.extract(
                item.content,
                title=str(
                    item.metadata.get("title", "")
                    if hasattr(item, "metadata") else ""),
                cfg={**extraction_cfg, **llm_cfg},
            )
            entities.materialize(
                channel=channel,
                item_id=item.key,
                content_hash=item_hash.get(item.key, ""),
                extraction=extraction,
                run_id=run_id,
                captured_at=item.ts,
                driver=neo4j,
                cfg=extraction_cfg,
            )
        except Exception as exc:  # noqa: BLE001 — per-item isolation
            logging.warning(
                "entity extraction failed for item %r in channel %r: %s",
                item.key, channel, exc)
