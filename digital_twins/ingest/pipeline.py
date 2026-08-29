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
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.chunking import chunk_text
from digital_twins.ingest.embedding import DEFAULT_MODEL, model_dimension
from digital_twins.ingest.ids import point_id
from digital_twins.sources import UnknownSourceError, build as build_source
from digital_twins.state.models import (
    finish_audit_run,
    start_audit_run,
    upsert_highwater,
)


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

            chunks = []  # (point_id, chunk_index, item, text)
            for item in items:
                for i, text in enumerate(chunk_text(item.content, max_chars, overlap)):
                    pid = point_id(source.capability.prefix, item.key, i, text)
                    chunks.append((pid, i, item, text))

            if dry_run:
                total_points += len(chunks)
                continue

            if chunks:
                vectors = embedder([text for *_, text in chunks])
                points = []
                for (pid, i, item, text), vector in zip(chunks, vectors):
                    payload = {
                        "source": name,
                        "source_url": f"{source.capability.prefix}{item.key}",
                        "item_key": item.key,
                        "chunk_index": i,
                        "ts": item.ts,
                        "text": text,
                    }
                    # R6 (003 multi-user): stamp the owner + owner_tag on the
                    # point payload when owner is set (query-time filter for
                    # per-user scoping, SC-005). When owner is None the
                    # payload is unchanged (001/002 behavior).
                    # The owner tag is a payload field only — it does NOT
                    # enter the point ID (dedup is content-level, NFR-1).
                    if owner is not None:
                        payload["owner"] = owner
                        payload["owner_tag"] = f"{owner}-ingest"
                    points.append(qm.PointStruct(
                        id=pid,
                        vector=vector,
                        payload=payload,
                    ))
                client.upsert(QDRANT_COLLECTION, points=points, wait=True)
                total_points += len(points)
                if neo4j is not None:
                    _upsert_graph(neo4j, name, source.capability.prefix, chunks)

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


def _upsert_graph(neo4j, name: str, prefix: str, chunks) -> None:
    """Minimal graph shape: one node per item, one per chunk, HAS_CHUNK links."""
    seen_items = set()
    for pid, i, item, text in chunks:
        url = f"{prefix}{item.key}"
        if url not in seen_items:
            neo4j.run(
                "MERGE (n:KbItem {source_url: $u}) "
                "SET n.source = $s, n.item_key = $k, n.ts = $ts",
                u=url, s=name, k=item.key, ts=item.ts)
            seen_items.add(url)
        neo4j.run(
            "MERGE (c:KbChunk {id: $id}) SET c.text = $t "
            "WITH c MERGE (i:KbItem {source_url: $u}) "
            "MERGE (i)-[:HAS_CHUNK]->(c)",
            id=pid, t=text, u=url)
