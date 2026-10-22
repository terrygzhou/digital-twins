"""Ingestion pipeline: read -> chunk -> embed -> upsert -> high-water -> audit.

Fail-fast (cli.md): every enabled source is prerequisite-checked BEFORE any
endpoint is touched; a missing prerequisite raises PrerequisiteError (the CLI
maps it to exit 2) and the run is audited as `failed`.

Dedup (NFR-1): point IDs are deterministic (prefix|item_key|chunk|hash), so
re-reading the same content upserts the same points instead of duplicating
them. High-water marks additionally skip unchanged items.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from qdrant_client import models as qm

from digital_twins.config.schema import get
from digital_twins.health import QDRANT_COLLECTION, preflight, qdrant_collection
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
    acquire_ingest_lock,
    finish_audit_run,
    start_audit_run,
    record_audit_item,
    release_ingest_lock,
    upsert_highwater,
)
from digital_twins.ingest import entities  # s4-entity-extraction

logger = logging.getLogger(__name__)


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


def _ensure_collection(client, dim: int, coll: str = QDRANT_COLLECTION) -> None:
    """Create the collection at `dim` if missing.

    The dimension guard (verify existing collection == pinned dim, raise
    DimensionMismatchError) is the caller's responsibility: see
    `assert_dimension` below. `_ensure_collection` only handles the
    not-yet-created case so callers that pre-check can stay minimal.
    `coll` is the collection name (resolved via the `qdrant.collection`
    knob; default keeps baseline parity).
    """
    if not client.collection_exists(coll):
        client.create_collection(
            coll,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )


def assert_dimension(client, dim: int, coll: str = QDRANT_COLLECTION) -> None:
    """Guard: existing collection dimension must equal the pinned `dim`.

    Raises DimensionMismatchError (naming both dimensions + remediation)
    if the collection exists at a different size. No-op when the
    collection does not exist yet (it is created at `dim` on first run).
    `coll` is the collection name (resolved via the `qdrant.collection`
    knob; default keeps baseline parity).
    """
    if not client.collection_exists(coll):
        return
    info = client.get_collection(coll)
    vectors = info.config.params.vectors
    if hasattr(vectors, "size"):  # single-vector collection
        actual = vectors.size
    else:
        first = next(iter(vectors.values()), None)
        actual = first.size if first is not None else None
    if actual != dim:
        raise DimensionMismatchError(coll, actual, dim)


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
    injected_sources: dict | None = None,
) -> RunSummary:
    """Run one ingestion pass over the enabled (or named) sources.

    `qdrant` is a client or a zero-arg factory (resolved only after the
    prerequisite check passes). `embedder` maps list[str] -> list of vectors
    and must be lazy about its own heavy setup. `neo4j` is a
    driver-shaped object exposing the Neo4j session API
    (``.session()`` returning a context-manager whose ``.run()``
    executes Cypher — the surface of a raw
    ``neo4j.GraphDatabase.driver``) (None = skip graph writes, i.e.
    "Qdrant-only mode" when Neo4j is unconfigured).

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

    `injected_sources` (feature 016): a call-time ``{name:
    Source-instance}`` mapping — one-shot sources that take their
    config at call time (``kb_ingest_folder``'s folder path) instead of
    from a config entry.  Injected names are resolved from this mapping
    first; any name not in it still resolves through the config (FR-7:
    the config-fixed ``fs`` source and ``kb_ingest`` are unchanged).
    Dedup is unaffected — content-hash dedup applies exactly as for
    config sources.
    """
    run_id = str(uuid.uuid4())
    built: list = []
    counts: dict = {}
    start_audit_run(db, run_id, trigger=trigger, scheduled_by=scheduled_by)
    try:
        if not dry_run:
            # 008 US1 gate: hard dependencies, before any write. dry-run
            # skips the gate: it consumes no service capacity and writes
            # nothing (diff-review P2 — a preview must not fail-fast on
            # live services). Moved inside the audited block (BUG-2):
            # the gate now audits a `failed` row instead of escaping the
            # audit path — every failure is reported, never silent.
            # ServiceDependencyError (008 gate) is re-raised by the
            # handler below; the audit row still lands.
            preflight(cfg)
        if source_names is None:
            source_names = [n for n, e in cfg["sources"].items()
                            if e.get("enabled")]
        # fail-fast: check every enabled source before touching anything
        injected = injected_sources or {}
        for name in source_names:
            if name in injected:
                # Feature 016: call-time one-shot source (no config
                # entry; the instance carries its own config, e.g. the
                # folder path from kb_ingest_folder's tool args).
                source = injected[name]
            else:
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
            coll = qdrant_collection(cfg)
            assert_dimension(client, expected_dim, coll)
            _ensure_collection(client, expected_dim, coll)

        total_points = 0
        for name, source in built:
            entry = cfg["sources"].get(name) or {}
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

            # Sub-batch commit loop (feature 019 — ingest reliability).
            # Each sub-batch: embed → Qdrant upsert → graph upsert →
            # extraction fan-out → highwater checkpoint.  A crash loses
            # at most one sub-batch and the next run resumes from the
            # last committed sub-batch.
            # ``sub_batch_size`` 0 or unset = legacy single-batch.
            # Item-level content hash (S4): shared by every chunk of one
            # item — the staleness signal that replaces the legacy
            # content-dependent point ID.
            item_hash = {item.key: content_hash(item.content)
                         for item in items}
            embed_model = get(cfg, "embedding.model") or DEFAULT_MODEL

            sub_size = int(get(cfg, "pipeline.sub_batch_size") or 0)
            if sub_size <= 0:
                sub_batches = [chunks]
            else:
                sub_batches = [
                    chunks[i:i + sub_size]
                    for i in range(0, len(chunks), sub_size)
                ]
            total_points = 0
            # Single-writer ingest lock (issue 5-A): serialize overlapping
            # full-tree runs on this source so 2x contention cannot
            # stack.  The lock is released in the finally block.
            if not dry_run:
                if not acquire_ingest_lock(db, name, run_id):
                    logger.warning(
                        "source %r: another run already holds the "
                        "ingest lock; skipping this source for run %s",
                        name, run_id)
                    continue
            for sub_idx, sub in enumerate(sub_batches):
                sub_points = []
                sub_items: list = []          # de-duplicated item objects
                sub_item_seen: set = set()    # item keys in this sub-batch
                sub_vectors = embedder([c[3] for c in sub])
                # Build points for this sub-batch
                for (pid, i, item, text, n), vector in zip(sub, sub_vectors):
                    if item.key not in sub_item_seen:
                        sub_items.append(item)
                        sub_item_seen.add(item.key)
                    payload = {
                        "source": name,
                        "source_url": f"{source.capability.prefix}{item.key}",
                        "item_key": item.key,
                        "chunk_index": i,
                        "ts": item.ts,
                        "text": text,
                        "item_id": item.key,
                        "content_hash": item_hash[item.key],
                        "full_content": text,
                        "content_snippet": text[:200],
                        "captured_at": item.ts,
                        "total_chunks": n,
                        "embed_model": embed_model,
                        "source_type": name,
                        "source_title": item.metadata.get("title", "") if hasattr(item, "metadata") else "",
                        "tags": item.metadata.get("tags", []) if hasattr(item, "metadata") else [],
                        "meta": {
                            "owner": owner,
                            "owner_tag": f"{owner}-ingest" if owner else None,
                        },
                        "run_id": run_id,
                        "trigger": trigger,
                    }
                    sub_points.append(qm.PointStruct(
                        id=pid,
                        vector=vector,
                        payload=payload,
                    ))
                client.upsert(coll, points=sub_points, wait=True)
                total_points += len(sub_points)
                if sub_points:
                    if neo4j is not None:
                        _upsert_graph(neo4j, name, sub_items, item_hash)
                        _run_extraction(cfg, neo4j, sub_items, item_hash,
                                        run_id, name, db=db)
                    elif sub_idx == 0:
                        # preflight-optional-deps: Qdrant-only mode -
                        # Neo4j was unconfigured (soft dependency), so
                        # no graph writes. Logged once per source.
                        logger.info(
                            "Qdrant-only mode: Neo4j not configured - "
                            "skipping graph writes")
                # Per-sub-batch highwater checkpoint (feature 019-A)
                for item in sub_items:
                    upsert_highwater(db, name, item.key, item.ts)
                # Progress log
                logger.info(
                    "sub-batch %d/%d committed: %d points (source=%s)",
                    sub_idx + 1, len(sub_batches), len(sub_points), name,
                )

        status = "ok"
        finish_audit_run(db, run_id, status, counts)
        # Release ingest locks held by this run (issue 5-A).
        for name2, _ in built:
            release_ingest_lock(db, name2, run_id)
        return RunSummary(run_id, counts, total_points, status)
    except Exception:
        finish_audit_run(db, run_id, "failed", counts)
        for name2, _ in built:
            release_ingest_lock(db, name2, run_id)
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
        _execute_cypher(
            neo4j,
            "MERGE (si:SourceItem {item_id: $id}) "
            "SET si.channel = $ch, si.content_hash = $hash",
            id=item.key, ch=channel,
            hash=item_hash[item.key])


def _ensure_graph_schema(neo4j) -> None:
    """Idempotent schema bootstrap for the S4 graph.

    Secondary indexes on ``SourceItem.item_id`` (the join key into the
    Qdrant payload ``item_id``) and on ``SourceItem.channel`` (the
    provenance value used by channel-scoped graph reads).

    Since 018 (REL edge materialization) the graph also carries
    ``:Entity`` nodes and ``:REL`` edges. Their hot-path lookup keys:

    - ``Entity(name, type)`` — the composite key ``materialize()``
      MERGEs and ``graph_query`` / ``supersede`` look up; the index is
      composite to match that exact predicate.
    - ``REL.source_item`` — the property ``supersede()`` / ``drop()``
      / per-item reconcile filter by (``WHERE r.source_item = $item_id``).

    ``CREATE INDEX IF NOT EXISTS`` is a no-op on an already-created
    index, so this is safe to run on every graph-enabled pipeline pass.
    """
    _execute_cypher(
        neo4j,
        "CREATE INDEX IF NOT EXISTS FOR (si:SourceItem) ON (si.item_id)")
    _execute_cypher(
        neo4j,
        "CREATE INDEX IF NOT EXISTS FOR (si:SourceItem) ON (si.channel)")
    _execute_cypher(
        neo4j,
        "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name, e.type)")
    _execute_cypher(
        neo4j,
        "CREATE INDEX IF NOT EXISTS FOR (r:REL) ON (r.source_item)")


def _execute_cypher(neo4j, cypher: str, **params) -> None:
    """Execute one Cypher statement against the Neo4j driver.

    BUG-01 (graph-driver-fix): the graph-write helpers previously called
    ``neo4j.run(...)`` directly on the driver, but a real
    ``neo4j.GraphDatabase.driver`` (the object
    ``scheduler.loop.build_neo4j_driver`` returns) has no driver-level
    ``.run()`` — queries go through the session API::

        with driver.session() as s:
            s.run(cypher, **params)

    This helper is the single session-API choke point for every S4
    graph write; the test-only driver-level ``.run()`` surface is no
    longer part of the pipeline hand-off contract.
    """
    with neo4j.session() as s:
        s.run(cypher, **params)


def _run_extraction(
    cfg: dict,
    neo4j,
    items: list,
    item_hash: dict,
    run_id: str,
    channel: str,
    db=None,
) -> None:
    """Post-graph LLM extraction step (s4-entity-extraction).

    Config-gated: when ``extraction.enabled`` is false (the default) or
    ``neo4j`` is None, this is a no-op. When enabled and a driver is
    present, for each ingested item:

    1. ``entities.extract(item.content, title=..., cfg=extraction_cfg)``
       — one LLM call per item (isolated: failures are logged, not
       raised).
    2. ``entities.supersede(item.key, driver=neo4j, cfg=extraction_cfg)``
       — stamps ``valid_to`` on live MENTIONED/REL edges (BR-6.4).
       Runs only after a successful extract (019b, issue 6): a failed
       extraction mutates no graph state, so prior live edges stay
       live and re-ingest stays idempotent-by-default.
    3. ``entities.materialize(...)`` — idempotent MERGE into the graph.

    Per-item failure isolation: an item whose extraction fails logs a
    warning and the remaining items proceed (matching personal-kb's
    ``post_sweep`` isolation pattern).

    ``extraction.concurrency`` (feature 019, FR-3): when > 1, the
    per-item ``extract -> supersede -> materialize`` block is fanned
    out over a ``ThreadPoolExecutor(max_workers=concurrency)``;
    results are joined in submission order so deterministic MERGEs
    materialize the same entity set as the serial path.  Per-item
    isolation still holds — a failed item logs a warning and the
    rest proceed.
    """
    import logging

    extraction_cfg = {
        "enabled": bool(get(cfg, "extraction.enabled")),
        "max_text_chars": get(cfg, "extraction.max_text_chars") or 12000,
        "prompt_version": get(cfg, "extraction.prompt_version") or "",
        "domain_schema": bool(get(cfg, "extraction.domain_schema")),
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
            "extraction enabled but llm.endpoint not configured - "
            "skipping entity extraction")
        return

    def process_item(item) -> None:
        """One item's extract -> supersede -> materialize, isolated.

        019b (issue 6): supersede runs only AFTER a successful
        extract, so a failed LLM call mutates no graph state (the
        item's prior live MENTIONED/REL edges stay live).  A failure
        logs a warning, records a failed audit_items row, and the
        remaining items proceed (per-item isolation).
        """
        try:
            extraction = entities.extract(
                item.content,
                title=str(
                    item.metadata.get("title", "")
                    if hasattr(item, "metadata") else ""),
                cfg={**extraction_cfg, **llm_cfg},
            )
            # 019b (issue 6): supersede only after a successful
            # extract - a failed LLM call mutates no graph state,
            # so prior live MENTIONED/REL edges stay live and
            # re-ingest stays idempotent-by-default.
            entities.supersede(item.key, driver=neo4j, cfg=extraction_cfg)
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
            if db is not None:
                record_audit_item(db, run_id, channel, item.key, status="ok")
        except Exception as exc:  # noqa: BLE001 - per-item isolation
            logging.warning(
                "entity extraction failed for item %r in channel %r: %s",
                item.key, channel, exc)
            if db is not None:
                record_audit_item(
                    db, run_id, channel, item.key,
                    status="failed", error=str(exc))

    concurrency = int(get(cfg, "extraction.concurrency") or 1)
    if concurrency <= 1:
        for item in items:
            process_item(item)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(process_item, items))

def heal_extraction(
    cfg: dict,
    db,
    qdrant,
    neo4j,
    run_id: str,
) -> dict:
    """Targeted heal of failed graph extractions (019b, FR-3).

    Re-extracts only the latest-per-item ``audit_items`` rows in
    ``failed`` state (no full re-walk), superseding + materializing
    only on success so it is safe to re-run: an item that failed
    again just gets a fresh failed audit row under this run's id.
    Locked sources are skipped while another run holds their
    ``ingest_locks`` row.  No new knobs, no DDL.

    Per item: read the chunk-0 point payload from Qdrant for the
    item content, ``entities.extract`` (isolated), then
    ``entities.supersede`` + ``entities.materialize`` on success.
    Returns ``{"healed": n, "failed": m, "skipped": [source...]}``.
    """
    result = {"healed": 0, "failed": 0, "skipped": []}
    if not bool(get(cfg, "extraction.enabled")) or neo4j is None:
        return result
    llm_cfg = {
        "endpoint": get(cfg, "llm.endpoint"),
        "model": get(cfg, "llm.model"),
        "api_key": get(cfg, "llm.api_key"),
    }
    if not llm_cfg["endpoint"]:
        return result
    extraction_cfg = {
        "enabled": True,
        "max_text_chars": get(cfg, "extraction.max_text_chars") or 12000,
        "prompt_version": get(cfg, "extraction.prompt_version") or "",
        "domain_schema": bool(get(cfg, "extraction.domain_schema")),
    }
    rows = db.execute(
        "SELECT source, item_key, status FROM audit_items WHERE rowid IN "
        "(SELECT MAX(rowid) FROM audit_items GROUP BY source, item_key)"
    ).fetchall()
    pending = [(r[0], r[1]) for r in rows if r[2] == "failed"]
    if not pending:
        return result
    by_source: dict[str, list] = {}
    for source, key in pending:
        by_source.setdefault(source, []).append(key)
    for source, keys in sorted(by_source.items()):
        holder = db.execute(
            "SELECT run_id FROM ingest_locks WHERE source=?", (source,)
        ).fetchone()
        if holder is not None and holder[0] != run_id:
            result["skipped"].append(source)
            continue
        for key in sorted(keys):
            pid = point_id_s4(source, key, 0)
            payload: dict = {}
            try:
                resp = qdrant.retrieve(
                    QDRANT_COLLECTION, ids=[pid], with_payload=True)
                # qdrant_client.retrieve returns a list (newer versions)
                # or an object with .points (older); handle both.
                pts = resp if isinstance(resp, list) else (
                    getattr(resp, "points", None) or [])
                for pt in pts:
                    if getattr(pt, "id", None) == pid:
                        payload = dict(getattr(pt, "payload", None) or {})
                        break
            except Exception:  # noqa: BLE001 - heal is best-effort
                pass
            content = payload.get("full_content") or payload.get("text")
            if not content:
                record_audit_item(
                    db, run_id, source, key,
                    status="failed",
                    error="no content in Qdrant payload for heal")
                result["failed"] += 1
                continue
            try:
                extraction = entities.extract(
                    content,
                    title=str(payload.get("source_title", "")),
                    cfg={**extraction_cfg, **llm_cfg},
                )
                entities.supersede(key, driver=neo4j, cfg=extraction_cfg)
                entities.materialize(
                    channel=source,
                    item_id=key,
                    content_hash=str(payload.get("content_hash", "")),
                    extraction=extraction,
                    run_id=run_id,
                    captured_at=str(payload.get("captured_at", "")),
                    driver=neo4j,
                    cfg=extraction_cfg,
                )
                record_audit_item(db, run_id, source, key, status="ok")
                result["healed"] += 1
            except Exception as exc:  # noqa: BLE001 - per-item isolation
                record_audit_item(
                    db, run_id, source, key,
                    status="failed", error=str(exc))
                result["failed"] += 1
    return result
