"""Fresh caps per fire (002, T015, SC-005 / FR-6).

An operator who edits ``kb.yml`` (or sets a ``KB_`` env var) to lower a
source's ``max_items`` sees the new cap on the NEXT fire, without restarting
``serve``. ``serve_once_tick`` (T006) re-reads config on every tick, and
``run_pipeline`` reads the per-source ``max_items`` from the config dict on
every call — so the cap is never cached across fires.

This test proves the OBSERVABLE:
    - A test source with 50 known items.
    - A schedule with ``max_items=5`` in config.
    - Fire 1: ``serve_once_tick`` → processes 5 items (respects the cap).
    - Config update: ``max_items=2`` (simulating an operator edit between fires).
    - Fire 2: ``serve_once_tick`` → processes only 2 more items (reads the
      FRESH cap, not the cached 5).
    - Assert: total points = 7 (5 + 2), NOT 10 (5 + 5).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from qdrant_client import QdrantClient

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.scheduler import loop, schedules
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

N_ITEMS = 50
CAP1 = 5   # first fire's cap
CAP2 = 2   # second fire's cap (lowered between fires)
EXPECTED_TOTAL = CAP1 + CAP2  # 7, not 10


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path, fs_dir, cap):
    """Config dict with a single enabled ``fs`` source over ``fs_dir``,
    ``max_items=cap``. Every other source is disabled."""
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs")}
    sources["fs"] = {"enabled": True, "max_items": cap, "timeout_s": 1500,
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


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_rows(db):
    """All audit rows, oldest first (rowid = insertion order)."""
    return db.execute(
        "SELECT run_id, status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs ORDER BY rowid"
    ).fetchall()


def _create_due_schedule(db, now, *, owner="system", source="fs",
                         preset="every-N-hours", param=1, fire_time="03:00"):
    """Create a schedule and force ``next_fire_at`` into the past (due NOW).
    Returns the schedule id."""
    sched = schedules.create_schedule(
        db, owner, source, preset, param=param,
        fire_time=fire_time, now=now,
    )
    past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
        (past, sched["id"]),
    )
    db.commit()
    return sched["id"]


@pytest.fixture
def fs_dir(tmp_path):
    """50 fixture files, zero-padded (sort order == index order), each with
    distinct content and a strictly increasing mtime so the high-water cursor
    cleanly separates 'first N' from 'remaining' between fires."""
    d = tmp_path / "files"
    d.mkdir()
    for i in range(N_ITEMS):
        path = d / f"{i:03d}.txt"
        path.write_text(f"item {i:03d} payload", encoding="utf-8")
        mtime = time.time() + i * 3600
        os.utime(path, (mtime, mtime))
    return d


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "state")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def shared_qdrant(monkeypatch):
    """One in-memory Qdrant shared by both fire paths.

    ``serve_once_tick`` builds its own client via ``_qdrant_factory(config)``,
    which calls ``QdrantClient(url=...)``; monkeypatch the constructor so the
    tick's factory returns THIS in-memory instance. Also stub the embedder so
    the tick does not load the real sentence-transformers model.
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
# the test
# ---------------------------------------------------------------------------

def test_fresh_caps_no_restart(tmp_path, fs_dir, db, shared_qdrant):
    """Change ``max_items`` between two fires of the same schedule; the second
    fire honors the new cap without a restart.

    Fire 1 (max_items=5) processes 5 of 50 items.
    Fire 2 (max_items=2, after config edit) processes 2 more.
    Total = 7 points, NOT 10 (which would mean the old cap was cached).
    """
    # --- Fire 1: cap=5 ---
    cfg1 = _cfg(tmp_path, fs_dir, cap=CAP1)
    now1 = _now()
    sid = _create_due_schedule(db, now1)

    result1 = loop.serve_once_tick(db, cfg1)
    assert sid in result1["fired"], f"fire 1 not fired: {result1}"
    assert result1["skipped"] == []

    # 5 points after fire 1 (one chunk per file, cap respected)
    assert _point_count(shared_qdrant) == CAP1, (
        f"fire 1 should process {CAP1} items, got {_point_count(shared_qdrant)}")

    # fire 1's audit row: fs:5
    rows1 = _audit_rows(db)
    assert len(rows1) == 1
    counts1 = json.loads(rows1[0][4])
    assert counts1 == {"fs": CAP1}, f"fire 1 counts: {counts1}"

    # --- Config change: cap 5 -> 2 (operator edit between fires) ---
    # The schedule was advanced past its due time by fire 1; force it due again
    # to simulate the next tick.
    now2 = _now()
    db.execute(
        "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
        ((now2 - timedelta(minutes=5)).isoformat(timespec="seconds"), sid),
    )
    db.commit()
    cfg2 = _cfg(tmp_path, fs_dir, cap=CAP2)  # fresh config, cap=2

    # --- Fire 2: cap=2 ---
    result2 = loop.serve_once_tick(db, cfg2)
    assert sid in result2["fired"], f"fire 2 not fired: {result2}"
    assert result2["skipped"] == []

    # 2 more points after fire 2 (fresh cap respected, not the cached 5)
    assert _point_count(shared_qdrant) == EXPECTED_TOTAL, (
        f"fire 2 should process {CAP2} more items (total {EXPECTED_TOTAL}), "
        f"got {_point_count(shared_qdrant)} — the old cap ({CAP1}) was "
        f"cached instead of the fresh config")

    # fire 2's audit row: fs:2 (the NEW cap, not the old 5)
    rows2 = _audit_rows(db)
    assert len(rows2) == 2, f"expected 2 audit rows, got {len(rows2)}"
    counts2 = json.loads(rows2[1][4])
    assert counts2 == {"fs": CAP2}, (
        f"fire 2 counts: {counts2} — expected fs:{CAP2} (fresh cap), "
        f"got fs:{counts2.get('fs')} (cached cap?)")

    # both fires succeeded
    statuses = [r[1] for r in rows2]
    assert all(s in ("ok", "partial") for s in statuses), f"statuses: {statuses}"

    # the schedule was advanced after fire 2 (not re-due)
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()
    old_due2 = (now2 - timedelta(minutes=5)).isoformat(timespec="seconds")
    assert row[0] > old_due2, f"schedule not advanced after fire 2: {row[0]}"
