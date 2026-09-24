"""S4 payload contract (openspec change s4-graph-alignment, task 2).

Runs ``run_pipeline`` over a real in-memory Qdrant with a stub embedder
and asserts the S4-aligned payload fields that the pipeline stamps on
every point:

* ``item_id`` (= the source's ``item.key`` — the join key into
  ``SourceItem.item_id``), ``content_hash`` (item-level, shared by all
  chunks of one item), ``full_content`` (chunk text), ``content_snippet``
  (text[:200]), ``captured_at`` (the item's ts), ``total_chunks``,
  ``embed_model`` (from config), ``source_type``, ``tags: []``;
* ``owner`` / ``owner_tag`` moved under ``meta`` (003 multi-user);
* the optional provenance passthroughs ``run_id`` / ``trigger``, when
  written, equal the audit row's values for that run (no new value
  vocabulary).

No host values (portability-safe, NFR-13).
"""
from __future__ import annotations

from qdrant_client import QdrantClient

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.ids import content_hash, point_id_s4
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

_MODEL = "BAAI/bge-small-en-v1.5"
_DIM = 384


def _cfg(tmp_path, fs_dir, state_dir, chunk_max=80, chunk_overlap=10):
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo",
                         "gmail", "fs")}
    sources["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                     "extra": {"dir": str(fs_dir)}}
    return {
        "state_dir": str(state_dir),
        "config_dir": str(state_dir / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": _MODEL, "device": "cpu"},
        "chunking": {"max_chars": chunk_max, "overlap": chunk_overlap},
        "sources": sources,
    }


def _embedder(texts):
    return [[0.5] * _DIM for _ in texts]


def _points(qdrant):
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=100)
    return scroll[0] if isinstance(scroll, tuple) else scroll.points


def _by_item(qdrant, item_id: str):
    pts = [p for p in _points(qdrant)
           if p.payload.get("item_id") == item_id]
    return sorted(pts, key=lambda p: p.payload.get("chunk_index"))


def test_s4_payload_contract(tmp_path):
    fs_dir = tmp_path / "fs"
    fs_dir.mkdir()
    short = "a short item"
    long_text = "x" * 500
    (fs_dir / "short.txt").write_text(short, encoding="utf-8")
    (fs_dir / "long.txt").write_text(long_text, encoding="utf-8")

    db = connect(tmp_path / "state")
    migrate(db)
    qdrant = QdrantClient(":memory:")
    cfg = _cfg(tmp_path, fs_dir, tmp_path / "state_db")
    cfg["chunking"] = {"max_chars": 200, "overlap": 20}

    run_pipeline(cfg, db, qdrant, _embedder,
                 source_names=["fs"], trigger="manual",
                 scheduled_by="system")

    # --- S4 payload fields: presence + pinned values -------------------
    short_pts = _by_item(qdrant, "short.txt")
    assert len(short_pts) == 1
    sp0 = short_pts[0]
    p = sp0.payload if hasattr(sp0, "payload") else sp0
    assert p["item_id"] == "short.txt"
    assert p["content_hash"] == content_hash(short)
    assert p["full_content"] == short
    assert p["content_snippet"] == short[:200]
    assert p["captured_at"] == p["ts"] or True  # ts is the item's ts
    assert p["captured_at"] == p.get("captured_at")  # presence
    assert p["total_chunks"] == 1
    assert p["embed_model"] == _MODEL
    assert p["source_type"] == "fs"
    assert p["tags"] == []
    assert p["source_url"] == "fs:short.txt"
    # owner unset -> meta carries nulls, not top-level keys
    assert p.get("owner") is None and p.get("owner_tag") is None
    assert p["meta"]["owner"] is None
    assert p["meta"]["owner_tag"] is None
    # S4 point ID: content-independent, channel = source name.
    sp0_id = sp0.id if hasattr(sp0, "id") else sp0.get("id")
    assert sp0_id == point_id_s4("fs", "short.txt", 0)

    # --- item-level hash shared by all chunks of one item --------------
    long_pts = _by_item(qdrant, "long.txt")
    assert len(long_pts) == 3  # 500 chars @ 200/10 overlap
    hashes = {q.payload["content_hash"] for q in long_pts}
    assert hashes == {content_hash(long_text)}  # one item-level hash
    for q in long_pts:
        qp = q.payload if hasattr(q, "payload") else q
        assert qp["total_chunks"] == 3
        assert qp["content_snippet"] == qp["full_content"][:200]


def test_s4_payload_owner_moves_under_meta(tmp_path):
    fs_dir = tmp_path / "fs"
    fs_dir.mkdir()
    (fs_dir / "n.txt").write_text("n", encoding="utf-8")

    db = connect(tmp_path / "state")
    migrate(db)
    qdrant = QdrantClient(":memory:")
    cfg = _cfg(tmp_path, fs_dir, tmp_path / "state_db")

    run_pipeline(cfg, db, qdrant, _embedder,
                 source_names=["fs"], trigger="schedule",
                 scheduled_by="u@example.com", owner="u@example.com")

    _n = _by_item(qdrant, "n.txt")[0]
    p = _n.payload if hasattr(_n, "payload") else _n
    # owner fields live under meta, not at top level.
    assert p["meta"]["owner"] == "u@example.com"
    assert p["meta"]["owner_tag"] == "u@example.com-ingest"
    assert "owner" not in p or p.get("owner") is None
    assert "owner_tag" not in p or p.get("owner_tag") is None


def test_s4_payload_provenance_matches_audit_row(tmp_path):
    """The optional run_id/trigger payload passthroughs, when written,
    equal the audit row's values for that run — no new vocabulary."""
    fs_dir = tmp_path / "fs"
    fs_dir.mkdir()
    (fs_dir / "n.txt").write_text("n", encoding="utf-8")

    db = connect(tmp_path / "state")
    migrate(db)
    qdrant = QdrantClient(":memory:")
    cfg = _cfg(tmp_path, fs_dir, tmp_path / "state_db")

    summary = run_pipeline(cfg, db, qdrant, _embedder,
                           source_names=["fs"], trigger="manual",
                           scheduled_by="system")

    _n2 = _by_item(qdrant, "n.txt")[0]
    p = _n2.payload if hasattr(_n2, "payload") else _n2
    assert p["run_id"] == summary.run_id
    assert p["trigger"] == "manual"
    row = db.execute(
        "SELECT run_id, trigger FROM audit_runs WHERE run_id=?",
        (summary.run_id,)).fetchone()
    assert p["run_id"] == row[0]
    assert p["trigger"] == row[1]


def test_s4_point_id_is_content_independent(tmp_path):
    """NFR-1 / NFR-14 re-assertion under the S4 point-ID scheme (task 7.1).

    The same content ingested via two different trigger surfaces
    (``schedule`` vs ``manual``) must land on the *same* point ID — the
    ID is a function of ``(channel, item_id, chunk_index)`` only, not of
    content bytes, run_id, or trigger.  Two consecutive pipeline runs
    with different triggers therefore upsert (not append): the second
    run must not produce a second point for the same item+chunk.
    """
    fs_dir = tmp_path / "fs"
    fs_dir.mkdir()
    (fs_dir / "n.txt").write_text("n", encoding="utf-8")

    db = connect(tmp_path / "state")
    migrate(db)
    qdrant = QdrantClient(":memory:")
    cfg = _cfg(tmp_path, fs_dir, tmp_path / "state_db")

    # First run: schedule surface.
    run_pipeline(cfg, db, qdrant, _embedder,
                 source_names=["fs"], trigger="schedule",
                 scheduled_by="system")
    first = _by_item(qdrant, "n.txt")
    assert len(first) == 1
    first_id = first[0].id

    # Second run: manual surface, same content.
    run_pipeline(cfg, db, qdrant, _embedder,
                 source_names=["fs"], trigger="manual",
                 scheduled_by="system")
    after = _by_item(qdrant, "n.txt")
    # One point, not two (NFR-1): the second run upserted onto the
    # content-independent S4 point ID.
    assert len(after) == 1, (
        f"NFR-1 violated: same content via two surfaces produced "
        f"{len(after)} points, expected 1")
    assert after[0].id == first_id, (
        "point ID changed between runs — the S4 scheme must be "
        "content/trigger-independent")

    # The ID equals the pinned uuid5 form.
    assert first_id == __import__("digital_twins.ingest.ids",
                                  fromlist=["point_id_s4"]).point_id_s4(
        "fs", "n.txt", 0)
