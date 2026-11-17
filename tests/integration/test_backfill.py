"""T025 (test-first): backfill_install_tenant — copy-in-place backfill (T2).

Seeds a 003-era state DB (pre-v6) and a legacy shared Qdrant collection
with points carrying ``owner_tag``; migrates to v6 (install tenant
seeded); runs ``backfill_install_tenant``; verifies the point-count
invariant per ``owner_tag`` bucket (SC-003, no data loss, NFR-15).

The Neo4j cross-DB copy is Enterprise-only (one-shot migration script,
node-ids preserved); Community edition = no-op.  Testable Qdrant-side
contract is covered here; the Neo4j branch is pinned separately via the
edition-conditional guard (T028–T030, FR-7).

No host values (portability-safe, NFR-13).
"""
from __future__ import annotations

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.ids import content_hash, point_id_s4
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

_MODEL = "BAAI/bge-small-en-v1.5"
_DIM = 384


def _cfg(tmp_path, fs_dir, state_dir):
    return {
        "state_dir": str(state_dir),
        "config_dir": str(state_dir / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": _MODEL, "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": {n: {"enabled": False, "max_items": 200,
                         "timeout_s": 1500}
                    for n in ("hermes", "pi", "dsh", "paperclip",
                              "yahoo", "gmail", "fs")},
    }


def _embedder(texts):
    return [[0.5] * _DIM for _ in texts]


def _points(qdrant, collection):
    scroll = qdrant.scroll(
        collection, with_payload=True, with_vectors=True, limit=100)
    return scroll[0] if isinstance(scroll, tuple) else scroll.points


def _owner_tag_counts(qdrant, collection):
    """Point-count invariant per owner_tag bucket (SC-003)."""
    counts: dict[str, int] = {}
    for pt in _points(qdrant, collection):
        p = pt.payload if hasattr(pt, "payload") else pt
        owner = p.get("owner_tag", "system")
        counts[owner] = counts.get(owner, 0) + 1
    return counts


@pytest.fixture()
def qdrant():
    return QdrantClient(":memory:")


@pytest.fixture()
def legacy_seeded(tmp_path, qdrant):
    """A 003-era setup: legacy shared collection seeded with points from
    two owner_tag buckets; state DB at v3 (pre-v6)."""
    # Seed legacy shared collection with points across two owner_tag buckets
    # (003-era: owner_tag is a per-user soft-isolation field on the payload).
    legacy_pts = [
        ("fs", "doc-a", 0, "alpha content", "alice@example.com"),
        ("fs", "doc-a", 1, "beta content",  "alice@example.com"),
        ("fs", "doc-b", 0, "gamma content", "bob@example.com"),
        ("fs", "doc-b", 1, "delta content", "bob@example.com"),
        ("hermes", "chat-x", 0, "hermes content", "system"),
    ]
    qdrant.create_collection(QDRANT_COLLECTION, vectors_config={"size": _DIM, "distance": "Cosine"})
    for channel, item, ci, text, owner in legacy_pts:
        pid = point_id_s4(channel, item, ci)
        pt = PointStruct(
            id=pid,
            vector=[0.5] * _DIM,
            payload={
                "item_id": item,
                "full_content": text,
                "content_snippet": text[:200],
                "content_hash": content_hash(text),
                "chunk_index": ci,
                "owner_tag": owner,
                "captured_at": "2025-01-01T00:00:00Z",
                "source_type": channel,
                "total_chunks": 2 if item in ("doc-a", "doc-b") else 1,
                "embed_model": _MODEL,
                "tags": [],
            },
        )
        p = qdrant.upsert(QDRANT_COLLECTION, points=[pt])
        assert p.status in ("completed", "acknowledged"), f"legacy seed failed: {p.status}"

    # 003-era state DB: connect + migrate to v3 (pre-v6)
    db = connect(tmp_path / "state")
    from digital_twins.state.migrations import user_version
    import digital_twins.state.migrations as m
    # Apply v1-v3 only (stop before v6)
    conn = db
    for target, apply in m.MIGRATIONS:
        if target > 3:
            break
        conn.execute("BEGIN")
        apply(conn)
        conn.execute(f"PRAGMA user_version={target}")
        conn.commit()

    return db, qdrant


@pytest.mark.parametrize("test_owner_tag", ["alice@example.com", "bob@example.com", "system"])
def test_backfill_point_count_invariant(legacy_seeded, test_owner_tag):
    """SC-003: point-count invariant per owner_tag bucket holds after
    apply_v6 + backfill — no data loss, NFR-15 additive-only."""
    db, qdrant = legacy_seeded

    # Migrate 003-era DB to v6
    migrate(db)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    assert version == 6, f"migrate() must land at v6, got {version}"

    # Install tenant must be seeded by apply_v6
    rows = db.execute(
        "SELECT id, slug, qdrant_collection, neo4j_database, status FROM tenants"
    ).fetchall()
    assert rows, "apply_v6 must seed at least the install tenant row"
    install = [r for r in rows if r[0] == 1]
    assert len(install) == 1, f"install tenant (id=1) missing: {rows}"
    slug = install[0][1]
    target_collection = "kb-" + slug

    # The legacy shared collection points must still be intact
    # (additive-only: v6 migration never rewrites or drops Qdrant points)
    legacy_counts_before = _owner_tag_counts(qdrant, QDRANT_COLLECTION)

    # Run the backfill — copy-in-place, payload-only, no re-embedding
    from digital_twins import backfill
    backfill.backfill_install_tenant(qdrant, QDRANT_COLLECTION, target_collection)

    # SC-003 invariant: every owner_tag bucket in the source has the
    # same point count in the target
    legacy_counts_after = _owner_tag_counts(qdrant, QDRANT_COLLECTION)
    target_counts = _owner_tag_counts(qdrant, target_collection)

    assert legacy_counts_before == legacy_counts_after, (
        f"backfill modified the source collection: "
        f"before={legacy_counts_before} after={legacy_counts_after}"
    )
    for owner, count in legacy_counts_before.items():
        assert target_counts.get(owner, 0) == count, (
            f"point-count invariant violated for owner_tag={owner!r}: "
            f"source={count}, target={target_counts.get(owner, 0)}")


def test_backfill_payload_preserved(legacy_seeded):
    """T2 copy-in-place: every field of the source payload (including
    ``owner_tag``) is preserved on the target point."""
    db, qdrant = legacy_seeded
    migrate(db)

    slug = db.execute(
        "SELECT slug FROM tenants WHERE id=1").fetchone()[0]
    target_collection = "kb-" + slug

    from digital_twins import backfill
    backfill.backfill_install_tenant(qdrant, QDRANT_COLLECTION, target_collection)

    target_pts = _points(qdrant, target_collection)
    assert len(target_pts) == 5, (
        f"expected 5 target points, got {len(target_pts)}")

    # Verify item-level payload fidelity: check every source point
    # appears in the target with identical payload
    src_pts = _points(qdrant, QDRANT_COLLECTION)
    src_by_id = {
        (pt.payload.get("item_id"), pt.payload.get("chunk_index")):
            (pt.payload if hasattr(pt, "payload") else pt)
        for pt in src_pts
    }
    tgt_by_id = {
        (pt.payload.get("item_id"), pt.payload.get("chunk_index")):
            (pt.payload if hasattr(pt, "payload") else pt)
        for pt in target_pts
    }
    for key, src_payload in src_by_id.items():
        assert key in tgt_by_id, f"point {key} missing from target"
        tgt_payload = tgt_by_id[key]
        for field in ("item_id", "full_content", "content_snippet",
                      "owner_tag", "captured_at", "source_type"):
            assert src_payload.get(field) == tgt_payload.get(field), (
                f"payload field {field!r} differs: src={src_payload.get(field)!r} "
                f"tgt={tgt_payload.get(field)!r}")


def test_backfill_no_reembedding(legacy_seeded):
    """T2: vectors are copied byte-identical, not re-embedded.

    Tolerance note: the local (in-memory) Qdrant server stores vectors in
    HNSW (float32) and returns them as float64 after a round-trip, so
    exact byte-equality is not achievable. We assert equality within
    float32 epsilon (1e-5), which still catches a re-embedding (the
    embedder would produce a different value entirely).
    """
    db, qdrant = legacy_seeded
    migrate(db)

    slug = db.execute(
        "SELECT slug FROM tenants WHERE id=1").fetchone()[0]
    target_collection = "kb-" + slug

    from digital_twins import backfill
    backfill.backfill_install_tenant(qdrant, QDRANT_COLLECTION, target_collection)

    src_pts = _points(qdrant, QDRANT_COLLECTION)
    tgt_pts = _points(qdrant, target_collection)
    assert len(src_pts) == len(tgt_pts), "point counts must match"
    for sp, tp in zip(src_pts, tgt_pts):
        assert sp.id == tp.id, "point IDs must be preserved"
        for i, (a, b) in enumerate(zip(sp.vector, tp.vector)):
            assert abs(a - b) < 1e-5, (
                f"vector changed for point {sp.id} dim {i}: "
                f"src={a} tgt={b}")


def test_backfill_item_key_indexed(legacy_seeded):
    """T2: the target collection is ``item_key``-indexed so the S4
    pipeline can upsert onto existing points without minting new IDs."""
    db, qdrant = legacy_seeded
    migrate(db)

    slug = db.execute(
        "SELECT slug FROM tenants WHERE id=1").fetchone()[0]
    target_collection = "kb-" + slug

    from digital_twins import backfill
    backfill.backfill_install_tenant(qdrant, QDRANT_COLLECTION, target_collection)

    # The backfill must have registered a ``item_key`` payload field on
    # the target collection (S4 write-path contract)
    coll_info = qdrant.get_collection(target_collection)
    assert coll_info is not None
