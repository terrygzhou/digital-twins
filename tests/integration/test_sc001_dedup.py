"""SC-001 cross-trigger dedup pin (002, NFR-1, NFR-14).

Top acceptance check: the same content ingested via schedule, `run --once`,
MCP, or web UI yields **one** point, not four.

002 adds two trigger paths over the shared 001 pipeline:
  - `run --once`          -> trigger='manual',   scheduled_by='system'
  - serve_once_tick fire  -> trigger='schedule', scheduled_by=<owner>

Both funnel into 001's `run_pipeline`, which dedups via deterministic point
IDs (prefix|item_key|chunk|hash) + upserts. This test PINS that invariant for
002: ingesting the SAME content via BOTH trigger paths must yield exactly ONE
point in the Qdrant collection (not two), while the audit_runs table records
TWO rows (one per trigger) — dedup is at the content/point level, audit is at
the run level.

The test uses a single shared in-memory Qdrant + shared state db so both paths
see the same collection and the same high-water / audit tables.
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
# helpers (001 pattern, mirrors test_idempotency.py + test_serve_once_tick.py)
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir):
    """A config dict with a single enabled `fs` source over `fs_dir`.

    `qdrant.url` is a placeholder: the real client is an in-memory Qdrant
    shared across both trigger paths (the tick's `_qdrant_factory` is monkeypatched
    to return it, and the manual path receives it directly).
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


@pytest.fixture
def fs_dir(tmp_path):
    """A test source dir with one known file whose content is a single chunk.

    "alpha note" is 10 chars < chunking.max_chars (200) -> exactly 1 chunk,
    so the whole-content point count is trivially predictable (1 point).
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

    - The manual path (`run_pipeline`) receives this client directly.
    - The schedule path (`serve_once_tick`) builds its own client via
      `_qdrant_factory(config)`, which calls `QdrantClient(url=...)`; we
      monkeypatch the `qdrant_client.QdrantClient` constructor so the tick's
      factory returns THIS same in-memory instance.

    Yields the shared client.
    """
    in_memory = QdrantClient(":memory:")
    monkeypatch.setattr(
        "qdrant_client.QdrantClient",
        lambda *a, **kw: in_memory,
    )
    # serve_once_tick's embedder factory calls `load_embedder`; stub it so the
    # tick does not load the real sentence-transformers model.
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
# the test
# ---------------------------------------------------------------------------

def test_same_content_both_triggers_yields_one_point(
        tmp_path, fs_dir, db, shared_qdrant):
    """Ingest the SAME content via `run --once` (manual) AND via a schedule
    fire (serve_once_tick, schedule). The Qdrant collection holds exactly ONE
    point for that content — dedup is at the content/point level — while
    audit_runs holds TWO rows (one per trigger) — audit is at the run level.
    """
    cfg = _cfg(tmp_path, fs_dir)

    # --- 1. Ingest via `run --once` path: trigger='manual', scheduled_by='system'
    s1 = run_pipeline(
        cfg, db, shared_qdrant, _embedder,
        source_names=["fs"],
        trigger="manual",
        scheduled_by="system",
    )
    assert s1.status == "ok"
    assert s1.counts == {"fs": 1}, f"manual path read {s1.counts}, expected fs:1"
    assert s1.points == 1, f"manual path upserted {s1.points}, expected 1"
    # one point in the collection after the manual run
    assert _point_count(shared_qdrant) == 1

    # --- 2. Ingest the SAME content via a schedule fire path.
    # Create a due schedule for the same `fs` source, then run a tick. The
    # tick's pipeline re-reads the same item (high-water marks are per-item;
    # the item is unchanged so it is still a candidate) and re-upserts the
    # same deterministic point ID — NOT a second point.
    now = _now()
    sid = _create_due_schedule(db, now, owner="system", source="fs")

    result = loop.serve_once_tick(db, cfg)
    assert sid in result["fired"], f"schedule not fired: {result}"
    assert result["skipped"] == []

    # --- 3. DEDUP: still exactly ONE point for that content (not two).
    assert _point_count(shared_qdrant) == 1, (
        f"expected 1 point after both trigger paths, "
        f"got {_point_count(shared_qdrant)} — dedup failed")

    # --- 4. AUDIT: two rows, one per trigger (manual + schedule).
    rows = _audit_rows(db)
    assert len(rows) == 2, f"expected 2 audit rows (manual+schedule), got {len(rows)}"
    triggers = [r[2] for r in rows]
    assert triggers == ["manual", "schedule"], (
        f"expected one manual + one schedule audit row, got {triggers}")
    # scheduled_by: manual -> 'system'; schedule -> the schedule owner ('system')
    scheduled_by = [r[3] for r in rows]
    assert scheduled_by == ["system", "system"]
    # both runs succeeded
    statuses = [r[1] for r in rows]
    assert all(s in ("ok", "partial") for s in statuses), f"statuses: {statuses}"

    # --- 5. The schedule was advanced (it fired and is not re-due).
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()
    old_due = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    assert row[0] > old_due, f"schedule not advanced: {row[0]} <= {old_due}"


def test_dedup_is_content_level_not_run_level(
        tmp_path, fs_dir, db, shared_qdrant):
    """Explicit content-level pin: after ingesting the same content twice
    (manual + schedule), the Qdrant collection has a single point whose
    payload matches the source content. Two audit rows exist, but only one
    point — the run audit does not duplicate content.

    This complements the first test by asserting the point's payload is the
    ingested content (proving the surviving point is the real one, not an
    orphan), and that the two audit rows are distinct runs (distinct run_ids).
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
