"""T022 (003 multi-user, SC-005): owner-isolation integration test.

The SC-005 guarantee, stated as a query: given two users who have each
ingested their own content, a Qdrant ``scroll`` filtered on
``payload.owner == <user>`` (and on ``payload.owner_tag == <user>-ingest``)
returns **that user's content only** — the other user's points are excluded.
That is the whole multi-user data-isolation story in one query.

This is an invariant-verification test whose dependency (T016 ``run_pipeline``
owner-stamping and T017 per-owner serve merge) was merged first. Like T021,
the "red" is not a test that fails on the current code — it is the assertion
that *would* fail if the invariants were violated:

* If the owner payload field were missing / stamped on the wrong user,
  the owner-filtered scroll would return the wrong set (or nothing).
* If the owner tag accidentally became part of the **point ID** (a dedup
  key), the same content ingested by alice AND bob would yield **two**
  points instead of one — breaking NFR-1 (one record) / NFR-14.

The deterministic point ID is content-derived
(``point_id(prefix, item_key, chunk_index, text)`` — see
``digital_twins/ingest/ids.py``); the owner tag is a **payload field only**
(query-time filter) and never enters the ID. Both invariants are asserted
here; they hold on the current code (T016/T017 merged), so the suite is
green.
"""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.ids import content_hash, point_id_s4
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers (001/002 pattern — mirrors tests/integration/test_pipeline_owner.py)
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir, state_dir):
    """A config dict with a single enabled ``fs`` source over ``fs_dir``.

    ``state_dir`` is per-scenario so the two users' high-water marks and
    audit runs live in separate DBs (they do NOT share ingestion state);
    the shared object is the in-memory Qdrant, which is where the two
    users' content is co-resident and where isolation is asserted.
    ``qdrant.url`` is a placeholder: the real client is the in-memory
    Qdrant fixture.
    """
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
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": sources,
    }


def _embedder(texts):
    """Stub embedder: fixed 384-dim vectors (the pinned model dim)."""
    return [[0.5] * 384 for _ in texts]


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _all_points(qdrant):
    """All ``Record`` objects in the collection (handles both the
    ``scroll`` tuple and object return shapes)."""
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=100)
    return scroll[0] if isinstance(scroll, tuple) else scroll.points


def _scroll_payloads(qdrant):
    """Return the list of payload dicts for every point in the collection."""
    return [p.payload for p in _all_points(qdrant)]


def _owner_ids(qdrant, owner: str, tag: str | None = None):
    """Point IDs whose payload carries ``owner == <owner>``.

    The SC-005 query: a payload-field filter on ``owner`` (and, when a
    ``tag`` is given, on ``owner_tag`` as well). This is the multi-user
    isolation primitive — it must return exactly that user's points.
    """
    # S4 payload: owner fields moved under the `meta` payload subtree.
    conditions = [
        FieldCondition(key="meta.owner", match=MatchValue(value=owner))]
    if tag is not None:
        conditions.append(
            FieldCondition(key="meta.owner_tag",
                           match=MatchValue(value=tag)))
    scroll = qdrant.scroll(
        QDRANT_COLLECTION,
        with_payload=True,
        limit=100,
        scroll_filter=Filter(must=conditions),
    )
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    return sorted(p.id for p in points)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def alice_dir(tmp_path):
    """Alice's content: two distinct files, each a single chunk."""
    d = tmp_path / "alice_files"
    d.mkdir()
    (d / "a1.txt").write_text("alice one", encoding="utf-8")
    (d / "a2.txt").write_text("alice two", encoding="utf-8")
    return d


@pytest.fixture
def bob_dir(tmp_path):
    """Bob's content: two distinct files (each a single chunk).

    ``shared.txt`` holds content that BOTH users will also ingest — it is
    the NFR-1 dedup probe (one record regardless of owner).
    """
    d = tmp_path / "bob_files"
    d.mkdir()
    (d / "b1.txt").write_text("bob one", encoding="utf-8")
    (d / "shared.txt").write_text("shared secret", encoding="utf-8")
    return d


@pytest.fixture
def db_alice(tmp_path):
    conn = connect(tmp_path / "state_alice")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def db_bob(tmp_path):
    conn = connect(tmp_path / "state_bob")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def qdrant():
    """One in-memory Qdrant per test — fresh, no cross-test contamination.

    Both users' content is co-resident here; isolation is asserted by
    filtering this single collection on the owner payload field.
    """
    return QdrantClient(":memory:")


# ---------------------------------------------------------------------------
# T022 — SC-005: owner-isolation + NFR-1 dedup invariants
# ---------------------------------------------------------------------------

def test_owner_isolation_query_returns_each_users_content_only(
        tmp_path, alice_dir, bob_dir, db_alice, db_bob, qdrant):
    """SC-005: after both users have ingested, a Qdrant ``scroll`` filtered
    on ``payload.owner == <user>`` (and ``owner_tag == <user>-ingest``)
    returns that user's content only — the other user's points excluded.

    RED (what this test guards against): if the owner payload field were
    missing or stamped on the wrong user, the owner-filtered query for
    alice would return the wrong set (or zero points), and vice-versa for
    bob. The assertion set below fails in either case.
    """
    cfg_alice = _cfg(tmp_path, alice_dir, tmp_path / "state_alice")
    cfg_bob = _cfg(tmp_path, bob_dir, tmp_path / "state_bob")

    # Each user ingests their own content, stamped with their owner.
    sa = run_pipeline(cfg_alice, db_alice, qdrant, _embedder,
                      source_names=["fs"], trigger="manual",
                      scheduled_by="alice", owner="alice")
    sb = run_pipeline(cfg_bob, db_bob, qdrant, _embedder,
                      source_names=["fs"], trigger="manual",
                      scheduled_by="bob", owner="bob")
    assert sa.counts == {"fs": 2} and sa.points == 2
    assert sb.counts == {"fs": 2} and sb.points == 2
    # 4 co-resident points, 2 per user.
    assert _point_count(qdrant) == 4

    # --- SC-005 isolation: filter on owner == "alice" (and owner_tag) ---
    alice_ids = _owner_ids(qdrant, "alice", tag="alice-ingest")
    bob_ids = _owner_ids(qdrant, "bob", tag="bob-ingest")

    # alice's query returns exactly her 2 points — bob's are excluded.
    assert len(alice_ids) == 2, (
        f"owner filter returned {len(alice_ids)} points for alice, "
        f"expected 2 — isolation broken or owner stamp missing")
    assert len(bob_ids) == 2, (
        f"owner filter returned {len(bob_ids)} points for bob, "
        f"expected 2 — isolation broken or owner stamp missing")
    # The two users' point sets are disjoint (no cross-user leak).
    assert not (set(alice_ids) & set(bob_ids)), (
        f"a point is owned by BOTH alice and bob: "
        f"{set(alice_ids) & set(bob_ids)} — SC-005 isolation violation")
    # Together they account for the whole collection.
    assert set(alice_ids) | set(bob_ids) == {p.id for p in _all_points(qdrant)}

    # The content is actually each user's own (the owner field is a correct
    # discriminator, not just present).  S4 payload: the chunk text lives in
    # `full_content` and owner moved under `meta`.
    alice_texts = {p["full_content"] for p in _scroll_payloads(qdrant)
                   if p.get("meta", {}).get("owner") == "alice"}
    bob_texts = {p["full_content"] for p in _scroll_payloads(qdrant)
                 if p.get("meta", {}).get("owner") == "bob"}
    assert alice_texts == {"alice one", "alice two"}
    assert bob_texts == {"bob one", "shared secret"}


def test_same_content_ingested_by_two_users_yields_one_point(
        tmp_path, alice_dir, bob_dir, db_alice, db_bob, qdrant):
    """NFR-1 / NFR-14 dedup guard re-run: the same content ingested by
    alice AND bob yields **one** point, not two.

    The owner tag is a *payload field, not a dedup key*. The deterministic
    point ID is the S4 content-independent scheme (``point_id_s4``);
    the owner tag must NOT enter it. If it did, the same content under
    two owners would upsert two distinct points and this assertion fails.
    """
    # The content both users ingest — one file, identical item_key + text.
    # (bob_dir already contains "shared.txt" = "shared secret"; we add the
    # same logical content to alice's dir under the SAME item key so the two
    # runs target the identical deterministic point ID.)
    (alice_dir / "shared.txt").write_text("shared secret", encoding="utf-8")

    cfg_alice = _cfg(tmp_path, alice_dir, tmp_path / "state_alice")
    cfg_bob = _cfg(tmp_path, bob_dir, tmp_path / "state_bob")

    # alice ingests her 2 files + the shared one (3 points).
    sa = run_pipeline(cfg_alice, db_alice, qdrant, _embedder,
                      source_names=["fs"], trigger="manual",
                      scheduled_by="alice", owner="alice")
    assert sa.counts == {"fs": 3} and sa.points == 3

    # bob ingests his 2 files (b1 + the SAME shared.txt) (2 points).
    # The shared point is the SAME deterministic ID alice already wrote.
    sb = run_pipeline(cfg_bob, db_bob, qdrant, _embedder,
                      source_names=["fs"], trigger="manual",
                      scheduled_by="bob", owner="bob")
    assert sb.counts == {"fs": 2} and sb.points == 2

    # 3 (alice) + 2 (bob) items, but the shared one is ONE point: 4 total.
    assert _point_count(qdrant) == 4, (
        f"expected 4 points (3 alice + 2 bob − 1 shared dup), "
        f"got {_point_count(qdrant)} — the owner tag leaked into the "
        f"point ID and broke NFR-1 one-record dedup")

    # The shared point is content-independent and owner-independent:
    # exactly one such point exists, and its ID is the deterministic S4
    # scheme (fs source name -> channel "fs"; item.key "shared.txt").
    shared_pid = point_id_s4("fs", "shared.txt", 0)
    shared = [p.id for p in _all_points(qdrant)
              if p.payload.get("item_id") == "shared.txt"]
    assert len(shared) == 1, (
        f"the shared content produced {len(shared)} points, expected 1 "
        f"(one record regardless of owner — NFR-1)")
    assert shared[0] == shared_pid, (
        f"shared point ID {shared[0]!r} != S4-deterministic "
        f"{shared_pid!r} — owner tag entered the point ID")


def test_owner_tag_does_not_enter_point_id_across_owners(
        tmp_path, db_alice, db_bob, qdrant):
    """Structural guarantee behind NFR-1: two runs with different owners
    over the SAME content produce the SAME point ID.

    The point ID is a pure function of the S4 identity
    (channel, item_id, chunk_index) — not of the owner, and not of the
    content. This is the invariant that makes the one-record dedup hold;
    if the owner tag ever became part of the ID, this test would fail.
    """
    # One file, identical content, ingested by alice then by bob.
    d = tmp_path / "dup_files"
    d.mkdir()
    (d / "note.txt").write_text("one note only", encoding="utf-8")

    cfg_a = _cfg(tmp_path, d, tmp_path / "state_dup_a")
    cfg_b = _cfg(tmp_path, d, tmp_path / "state_dup_b")

    run_pipeline(cfg_a, db_alice, qdrant, _embedder,
                 source_names=["fs"], trigger="manual",
                 scheduled_by="alice", owner="alice")
    pid_after_alice = _point_count(qdrant)
    assert pid_after_alice == 1

    # Bob re-reads the SAME file as a fresh pipeline over the same dir.
    run_pipeline(cfg_b, db_bob, qdrant, _embedder,
                 source_names=["fs"], trigger="manual",
                 scheduled_by="bob", owner="bob")

    # Still one point: the owner did not create a second record.
    assert _point_count(qdrant) == 1, (
        f"expected 1 point after two owner runs over the same content, "
        f"got {_point_count(qdrant)} — owner tag is a dedup key")

    # And it is the deterministic S4 ID (content-independent).
    expected = point_id_s4("fs", "note.txt", 0)
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=10)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    assert points[0].id == expected, (
        f"point ID {points[0].id!r} != S4-deterministic {expected!r}")
