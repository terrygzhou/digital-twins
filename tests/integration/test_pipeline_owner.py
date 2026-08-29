"""T016 (R6/SC-005): run_pipeline owner= kwarg — owner + owner_tag payload.

Red tests first (constitution Test-First III):

1. ``run_pipeline(..., owner="alice")`` produces points whose Qdrant payload
   carries ``"owner": "alice"`` and ``"owner_tag": "alice-ingest"``.
2. ``run_pipeline(..., owner=None)`` (the default) produces points with
   **no** ``owner`` / ``owner_tag`` payload fields — 001/002 behavior
   is preserved byte-for-byte.
3. The one-record dedup invariant still holds: the owner tag is a payload
   field, **not** a dedup key. The same content ingested twice by two
   different owners yields exactly ONE point (the deterministic point ID is
   content-derived; the owner tag does not enter the ID).

These tests fail until ``run_pipeline`` accepts the ``owner`` kwarg.
"""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers (001 pattern — mirrors tests/integration/test_serve_once_dedup.py)
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir):
    """A config dict with a single enabled ``fs`` source over ``fs_dir``.

    ``qdrant.url`` is a placeholder: the real client is an in-memory Qdrant
    shared across both owner paths.
    """
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo",
                         "gmail", "fs")}
    sources["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                     "extra": {"dir": str(fs_dir)}}
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": sources,
    }


def _embedder(texts):
    """Stub embedder: fixed 384-dim vectors (the pinned model dim)."""
    return [[0.5] * 384 for _ in texts]


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _scroll_payloads(qdrant):
    """Return the list of payload dicts for every point in the collection."""
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=100)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    return [p.payload for p in points]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fs_dir(tmp_path):
    """A test source dir with one known file whose content is a single chunk.

    ``"alpha note"`` is 10 chars < chunking.max_chars (200) -> exactly 1
    chunk, so the whole-content point count is trivially predictable
    (1 point).
    """
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("alpha note", encoding="utf-8")
    return d


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "state")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def qdrant():
    """One in-memory Qdrant per test — fresh, no cross-test contamination."""
    return QdrantClient(":memory:")


# ---------------------------------------------------------------------------
# T016 red tests
# ---------------------------------------------------------------------------

def test_run_pipeline_owner_kwarg_stamps_owner_and_owner_tag(
        tmp_path, fs_dir, db, qdrant):
    """R6: run_pipeline(..., owner="alice") stamps the Qdrant point payload
    with ``"owner": "alice"`` and ``"owner_tag": "alice-ingest"``.

    The owner tag is a payload field (query-time filter) — it does NOT
    enter the deterministic point ID (which is content-derived).
    """
    cfg = _cfg(tmp_path, fs_dir)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="alice",
                     owner="alice")
    assert s.status == "ok"
    assert s.counts == {"fs": 1}
    assert s.points == 1

    payloads = _scroll_payloads(qdrant)
    assert len(payloads) == 1
    p = payloads[0]
    # R6: the owner field is stamped on the payload
    assert p["owner"] == "alice", f"owner missing/incorrect: {p}"
    # R6: the owner_tag field is "<owner>-ingest"
    assert p["owner_tag"] == "alice-ingest", f"owner_tag missing/incorrect: {p}"
    # the 001 payload fields are still present (unchanged)
    assert p["source"] == "fs"
    assert p["item_key"] == "a.txt"
    assert p["text"] == "alpha note"


def test_run_pipeline_owner_default_none_preserves_002_behavior(
        tmp_path, fs_dir, db, qdrant):
    """R6: run_pipeline(..., owner=None) (the default) produces points with
    **no** ``owner`` / ``owner_tag`` payload fields — 001/002 behavior
    is preserved byte-for-byte.

    The 001/002 payload shape (``source`` / ``source_url`` / ``item_key`` /
    ``chunk_index`` / ``ts`` / ``text``) is unchanged.
    """
    cfg = _cfg(tmp_path, fs_dir)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="system")
    # NOTE: owner kwarg is NOT passed — default None is exercised.
    assert s.status == "ok"
    assert s.points == 1

    payloads = _scroll_payloads(qdrant)
    assert len(payloads) == 1
    p = payloads[0]
    # R6: no owner / owner_tag fields when owner=None
    assert "owner" not in p, f"owner key unexpectedly present: {p}"
    assert "owner_tag" not in p, f"owner_tag key unexpectedly present: {p}"
    # the 001/002 payload shape is unchanged
    assert p["source"] == "fs"
    assert p["item_key"] == "a.txt"
    assert p["text"] == "alpha note"


def test_run_pipeline_owner_kwarg_does_not_break_dedup(
        tmp_path, fs_dir, db, qdrant):
    """R6/NFR-1: the owner tag is a payload field, NOT a dedup key.

    The same content ingested by two different owners (or the same owner
    twice) yields exactly ONE point — the deterministic point ID is
    content-derived (prefix|item_key|chunk|hash), so the owner tag does
    not enter the ID. This is the one-record dedup invariant from AGENTS.md.
    """
    cfg = _cfg(tmp_path, fs_dir)

    # First ingest: owner="alice"
    s1 = run_pipeline(cfg, db, qdrant, _embedder,
                      source_names=["fs"],
                      trigger="manual", scheduled_by="alice",
                      owner="alice")
    assert s1.points == 1
    assert _point_count(qdrant) == 1

    # Second ingest: owner="bob" over the SAME content.
    # The high-water mark has advanced, so `source.read(since)` returns no
    # new items — but even if it did re-read, the deterministic point ID
    # (content-derived) would upsert the same point, NOT a new one.
    s2 = run_pipeline(cfg, db, qdrant, _embedder,
                      source_names=["fs"],
                      trigger="manual", scheduled_by="bob",
                      owner="bob")
    # The second run adds NO new point (dedup at the content/point level).
    # s2.points reflects the items ingested this run (0 new, because the
    # high-water mark already advanced); the point count in Qdrant stays at 1.
    assert _point_count(qdrant) == 1, (
        f"expected 1 point after two owner-ingests, "
        f"got {_point_count(qdrant)} — dedup failed "
        f"(owner tag must NOT enter the point ID)")

    # Two distinct audit runs (one per owner), one Qdrant point.
    rows = db.execute(
        "SELECT run_id, trigger, scheduled_by FROM audit_runs "
        "ORDER BY rowid").fetchall()
    assert len(rows) == 2
    assert [r[2] for r in rows] == ["alice", "bob"]


def test_run_pipeline_owner_kwarg_does_not_enter_point_id(
        tmp_path, fs_dir, db, qdrant):
    """R6/NFR-1: two runs with different owners but the same content
    produce the same point ID — the owner tag is a payload field only.

    This is the structural guarantee that underpins the one-record dedup
    invariant: the point ID is a pure function of (prefix, item_key,
    chunk_index, text), not of the owner.
    """
    cfg = _cfg(tmp_path, fs_dir)

    # Ingest as alice, capture the point ID.
    s1 = run_pipeline(cfg, db, qdrant, _embedder,
                      source_names=["fs"],
                      trigger="manual", scheduled_by="alice",
                      owner="alice")
    assert s1.points == 1
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=10)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    alice_pid = points[0].id

    # Reset the high-water mark so the second run re-reads the same content
    # as bob (the content is identical; only the owner differs).
    db.execute("DELETE FROM highwater WHERE source='fs'")
    db.commit()

    s2 = run_pipeline(cfg, db, qdrant, _embedder,
                      source_names=["fs"],
                      trigger="manual", scheduled_by="bob",
                      owner="bob")
    # The second run upserts the SAME point ID (content-derived) — it adds
    # no new point.
    assert s2.points == 1, (
        f"expected 1 upserted point for bob, got {s2.points}")
    assert _point_count(qdrant) == 1, (
        f"expected 1 point total, got {_point_count(qdrant)} — "
        f"the owner tag leaked into the point ID")

    # The surviving point ID is identical (content-derived, owner-independent).
    scroll2 = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=10)
    points2 = scroll2[0] if isinstance(scroll2, tuple) else scroll2.points
    assert points2[0].id == alice_pid, (
        f"point ID changed between owner runs: "
        f"alice={alice_pid!r} vs bob={points2[0].id!r} — "
        f"owner must not enter the point ID")
