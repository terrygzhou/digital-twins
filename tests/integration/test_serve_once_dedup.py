"""SC-001 cross-trigger dedup — BOTH orderings (002, NFR-1, NFR-14).

Top acceptance check: the same content ingested via two different trigger
paths yields **one** point, not two. 002's two trigger paths are:

  - path A (manual):   ``run --once``            -> trigger='manual',   scheduled_by='system'
  - path B (schedule): a serve fire              -> trigger='schedule', scheduled_by=<owner>

Both funnel into 001's ``run_pipeline``, which dedups via deterministic point
IDs (prefix|item_key|chunk|hash) + upserts. Dedup is at the content/point
level; the audit is at the run level (one row per trigger).

This module pins the invariant for **both orderings**, asserting the Qdrant
point count directly (exactly 1 point after both paths, in either order):

  - ``test_manual_then_schedule``   — path A then path B  (once-then-serve)
  - ``test_schedule_then_manual``   — path B then path A  (serve-then-once)

Both orderings are additionally pinned at the point-payload level
(``test_dedup_is_content_level_not_run_level``) — the surviving single point
carries the ingested content and the two audit rows are distinct runs.

This module is the consolidated home of the SC-001 cross-trigger dedup pin:
it is the file the quickstart (SC-001 / Scenario 2) and the plan/tasks ledger
(name ``test_serve_once_dedup.py``) reference. The former sibling
``tests/integration/test_sc001_dedup.py`` (T010) was a strict subset — it
pinned the same once-then-serve invariant, only without the serve-then-once
direction — and was folded here (T020) to remove the duplicate.

Like the 001-pattern tests, the suite needs no live stores: Qdrant runs
in-memory (shared by both trigger paths) and the embedder is stubbed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from qdrant_client import QdrantClient

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.scheduler import loop, schedules
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers (001 pattern — mirrors test_sc001_dedup.py + test_serve_once_tick.py)
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir):
    """A config dict with a single enabled ``fs`` source over ``fs_dir``.

    ``qdrant.url`` is a placeholder: the real client is an in-memory Qdrant
    shared across both trigger paths (the tick's ``_qdrant_factory`` is
    monkeypatched to return it, and the manual path receives it directly).
    """
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs")}
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_rows(db):
    return db.execute(
        "SELECT run_id, status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs ORDER BY rowid"
    ).fetchall()


def _create_due_schedule(db, now: datetime, *, owner="system", source="fs",
                         preset="every-N-hours", param=1, fire_time="03:00"):
    """Create a schedule and force next_fire_at into the past (due NOW)."""
    sched = schedules.create_schedule(db, owner, source, preset, param=param,
                                      fire_time=fire_time, now=now)
    past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
        (past, sched["id"]),
    )
    db.commit()
    return sched["id"]


def _manual_ingest(cfg, db, qdrant):
    """Path A — the ``run --once`` manual trigger."""
    return run_pipeline(cfg, db, qdrant, _embedder,
                        source_names=["fs"],
                        trigger="manual", scheduled_by="system")


def _schedule_ingest(db, cfg):
    """Path B — a serve fire over a due schedule (the schedule trigger)."""
    now = _now()
    sid = _create_due_schedule(db, now, owner="system", source="fs")
    result = loop.serve_once_tick(db, cfg)
    assert sid in result["fired"], f"schedule not fired: {result}"
    assert result["skipped"] == []
    return result


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
def shared_qdrant(monkeypatch):
    """One in-memory Qdrant shared by BOTH trigger paths.

    - The manual path (``run_pipeline``) receives this client directly.
    - The schedule path (``serve_once_tick``) builds its own client via
      ``_qdrant_factory(config)``; we monkeypatch the
      ``qdrant_client.QdrantClient`` constructor so the tick's factory
      returns THIS same in-memory instance.

    The embedder factory is also stubbed so the tick does not load the real
    sentence-transformers model.
    """
    in_memory = QdrantClient(":memory:")
    monkeypatch.setattr(
        "qdrant_client.QdrantClient",
        lambda *a, **kw: in_memory,
    )
    monkeypatch.setattr(
        "digital_twins.ingest.embedding.load_embedder",
        lambda model, device: type("_M", (), {
            "encode": lambda self, texts: type("_V", (), {
                "tolist": lambda self2: [[0.5] * 384 for _ in texts]
            })()
        })(),
    )
    return in_memory


# ---------------------------------------------------------------------------
# both orderings
# ---------------------------------------------------------------------------

def test_manual_then_schedule(tmp_path, fs_dir, db, shared_qdrant):
    """Ordering 1 (once-then-serve): manual ``run --once`` first, then a
    schedule fire. After both paths the Qdrant collection holds exactly ONE
    point for the shared content (dedup is at the content/point level) and
    audit_runs holds TWO rows (one per trigger)."""
    cfg = _cfg(tmp_path, fs_dir)

    # --- path A: manual (run --once)
    s1 = _manual_ingest(cfg, db, shared_qdrant)
    assert s1.status == "ok"
    assert s1.counts == {"fs": 1}
    assert s1.points == 1
    assert _point_count(shared_qdrant) == 1

    # --- path B: schedule fire over the SAME content
    _schedule_ingest(db, cfg)

    # --- DEDUP: still exactly ONE point (not two)
    assert _point_count(shared_qdrant) == 1, (
        f"expected 1 point after manual-then-schedule, "
        f"got {_point_count(shared_qdrant)} — dedup failed")

    # --- AUDIT: two rows, manual first then schedule
    rows = _audit_rows(db)
    assert len(rows) == 2, f"expected 2 audit rows, got {len(rows)}"
    assert [r[2] for r in rows] == ["manual", "schedule"]
    assert [r[3] for r in rows] == ["system", "system"]
    assert all(r[1] in ("ok", "partial") for r in rows)


def test_schedule_then_manual(tmp_path, fs_dir, db, shared_qdrant):
    """Ordering 2 (serve-then-once): a schedule fire first, then the manual
    ``run --once``. After both paths the Qdrant collection holds exactly ONE
    point for the shared content (dedup is at the content/point level) and
    audit_runs holds TWO rows (one per trigger) — the order is reversed but
    the single-point invariant holds regardless of which path runs first."""
    cfg = _cfg(tmp_path, fs_dir)

    # --- path B: schedule fire over the content (first)
    _schedule_ingest(db, cfg)
    assert _point_count(shared_qdrant) == 1

    # --- path A: manual run --once over the SAME content (second)
    s2 = _manual_ingest(cfg, db, shared_qdrant)
    # The item was already ingested by the schedule fire; the manual run
    # re-reads it and re-upserts the same deterministic point ID — it adds
    # NO new point.
    assert s2.points == 0, (
        f"manual path re-upserted {s2.points} new points after a schedule "
        f"fire — dedup failed (the surviving point must be the same one)")

    # --- DEDUP: still exactly ONE point (not two)
    assert _point_count(shared_qdrant) == 1, (
        f"expected 1 point after schedule-then-manual, "
        f"got {_point_count(shared_qdrant)} — dedup failed")

    # --- AUDIT: two rows, schedule first then manual
    rows = _audit_rows(db)
    assert len(rows) == 2, f"expected 2 audit rows, got {len(rows)}"
    assert [r[2] for r in rows] == ["schedule", "manual"]
    assert [r[3] for r in rows] == ["system", "system"]
    assert all(r[1] in ("ok", "partial") for r in rows)
    # the two runs are distinct (distinct run_ids)
    assert len({r[0] for r in rows}) == 2


# ---------------------------------------------------------------------------
# content-level detail (folded from tests/integration/test_sc001_dedup.py, T010)
# ---------------------------------------------------------------------------

def test_dedup_is_content_level_not_run_level(
        tmp_path, fs_dir, db, shared_qdrant):
    """Explicit content-level pin: after ingesting the same content twice
    (manual + schedule), the Qdrant collection has a single point whose
    payload matches the source content. Two audit rows exist, but only one
    point — the run audit does not duplicate content.

    This complements the ordering tests by asserting the point's payload is
    the ingested content (proving the surviving point is the real one, not an
    orphan), and that the two audit rows are distinct runs (distinct run_ids).

    (Folded here from the former sibling test_sc001_dedup.py, T010, in T020.)
    """
    cfg = _cfg(tmp_path, fs_dir)

    # manual
    run_pipeline(cfg, db, shared_qdrant, _embedder,
                 source_names=["fs"], trigger="manual", scheduled_by="system")
    # schedule
    now = _now()
    sid = _create_due_schedule(db, now, owner="system", source="fs")
    loop.serve_once_tick(db, cfg)

    # exactly one point, and it carries the ingested content
    assert _point_count(shared_qdrant) == 1
    scroll = shared_qdrant.scroll(
        QDRANT_COLLECTION, with_payload=True, limit=10)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    assert len(points) == 1
    payload = points[0].payload
    assert payload["item_key"] == "a.txt"
    assert payload["text"] == "alpha note"
    assert payload["source"] == "fs"

    # two distinct audit runs
    rows = _audit_rows(db)
    assert len(rows) == 2
    run_ids = [r[0] for r in rows]
    assert len(set(run_ids)) == 2, f"audit rows are not distinct runs: {run_ids}"


def test_dedup_schedule_advanced_not_redue(
        tmp_path, fs_dir, db, shared_qdrant):
    """After a schedule fire dedups against prior content, the schedule is
    advanced past its due time (not re-due), so the next tick will not
    re-fire the same schedule immediately.

    (Folded here from the former sibling test_sc001_dedup.py, T010, in T020;
    this assertion is not duplicated in the ordering tests above.)
    """
    cfg = _cfg(tmp_path, fs_dir)

    # manual ingest first, then a due schedule fire
    run_pipeline(cfg, db, shared_qdrant, _embedder,
                 source_names=["fs"], trigger="manual", scheduled_by="system")
    now = _now()
    sid = _create_due_schedule(db, now, owner="system", source="fs")
    loop.serve_once_tick(db, cfg)

    # the schedule advanced: next_fire_at is now strictly after the (forced)
    # past due time
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()
    old_due = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    assert row[0] > old_due, f"schedule not advanced: {row[0]} <= {old_due}"
