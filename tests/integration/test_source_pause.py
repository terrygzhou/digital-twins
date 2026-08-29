"""Source pause honored mid-serve without restart (002, T016 — US5 / FR-7).

An operator pauses a source by setting ``sources.<name>.enabled: false`` in
the config. The scheduler must honor this on the NEXT ``serve_once_tick``
without restarting ``serve``:

- the due schedule for that source is SKIPPED — no fire, no pipeline run, no
  audit row, no new Qdrant points;
- the schedule row is NOT deleted and is NOT advanced (``next_fire_at`` stays
  put, so the schedule remains due and will fire when the source is re-enabled);
- the schedule's ``enabled`` column stays 1 (the schedule itself is still on;
  it is the *source* that is paused, not the schedule).

T006's ``serve_once_tick`` already checks ``config["sources"][src]["enabled"]``
before calling ``run_pipeline`` (loop.py), and T007 re-loads config fresh on
every tick — so a config-level pause takes effect on the next fire with no
restart. This integration test proves it end-to-end against a real in-memory
Qdrant and a real fs source (points are actually written on fire 1, and the
absence of new points on fire 2 is meaningful).

RED-first per the task brief: the assertions below fail if the pipeline does
not honor ``enabled: false`` (fire 2 would re-ingest and the point/audit
counts would change).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.scheduler import loop, schedules
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# --- harness (001 pattern, shared with test_idempotency.py) ------------------

def _cfg(tmp_path, fs_dir):
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


class _StubVectors:
    def __init__(self, data):
        self._data = data

    def tolist(self):
        return self._data


class _StubModel:
    """Mimics the sentence-transformers model shape that loop._embedder wraps:
    ``.encode(list[str])`` -> object with ``.tolist()`` -> list of vectors.
    (run_pipeline itself only needs a ``texts -> vectors`` callable; loop's
    factory turns the model into that callable, so the tick and the CLI agree.)
    """

    def encode(self, texts):
        return _StubVectors([[0.5] * 384 for _ in texts])


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _audit_rows(db):
    return db.execute(
        "SELECT run_id, status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs"
    ).fetchall()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def fs_dir(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("alpha " * 50, encoding="utf-8")   # 300 ch -> 2 chunks
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("beta note", encoding="utf-8")  # 1 chunk
    return d


def _due_schedule(db, now: datetime, *, owner="system", source="fs",
                   preset="every-N-hours", param=1, fire_time="03:00") -> int:
    """Create a schedule and force next_fire_at into the past (due NOW)."""
    sched = schedules.create_schedule(
        db, owner, source, preset, param=param, fire_time=fire_time, now=now)
    past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
               (past, sched["id"]))
    db.commit()
    return sched["id"]


def _make_due_again(db, schedule_id: int, now: datetime) -> str:
    """Re-assert a due next_fire_at (5 min in the past) and return it."""
    past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
               (past, schedule_id))
    db.commit()
    return past


# 1 — pause between two fires: second fire skips the paused source ----------

def test_source_pause_skips_schedule_on_second_fire(
        qdrant, tmp_path, fs_dir, monkeypatch):
    """US5 / FR-7: fire 1 (fs enabled) ingests and writes points; pausing fs in
    the config makes fire 2 SKIP the fs schedule — no new points, no new audit
    row for fs, schedule row intact and not advanced."""
    # serve_once_tick(db, config) resolves its own qdrant client + embedder
    # from config (like the CLI's `run` command). Point the tick at the
    # in-memory Qdrant fixture and a stub embedder (no model load).
    monkeypatch.setattr(
        "qdrant_client.QdrantClient",
        lambda *a, **kw: qdrant,
    )
    monkeypatch.setattr(
        "digital_twins.ingest.embedding.load_embedder",
        lambda model, device: _StubModel(),
    )

    cfg = _cfg(tmp_path, fs_dir)
    db = connect(tmp_path / "state")
    migrate(db)
    now = _now()
    sid = _due_schedule(db, now)

    # preconditions: fs enabled, schedule due
    assert cfg["sources"]["fs"]["enabled"] is True
    assert [s["id"] for s in schedules.due_schedules(db, _now())] == [sid]

    # --- Fire 1: fs enabled -> fires, ingests, writes points + audit row ---
    r1 = loop.serve_once_tick(db, cfg)
    assert sid in r1["fired"]
    assert r1["skipped"] == []
    points_after_fire1 = _point_count(qdrant)
    audit_after_fire1 = _audit_rows(db)
    assert points_after_fire1 >= 1            # real points written
    assert len(audit_after_fire1) == 1        # one audit row for the fire
    # the schedule advanced past the old due time
    fire1_next = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()[0]

    # --- Pause the source between the two fires ---------------------------
    # Operator edits the config: fs is now disabled. (In production T007
    # re-loads config fresh each tick; here we swap the resolved config in.)
    cfg_paused = copy.deepcopy(cfg)
    cfg_paused["sources"]["fs"]["enabled"] = False

    # Re-assert the schedule as due (fire 1 advanced it) so fire 2 has a
    # genuinely due schedule to skip.
    fire2_due = _make_due_again(db, sid, now)

    # --- Fire 2: fs paused -> schedule is SKIPPED -------------------------
    r2 = loop.serve_once_tick(db, cfg_paused)
    assert sid in r2["skipped"]
    assert r2["fired"] == []
    assert r2["queue_depth"] == 0

    # (a) total points unchanged from fire 1 (fire 2 added 0)
    assert _point_count(qdrant) == points_after_fire1
    # (b) no new audit row for the skipped fire
    assert len(_audit_rows(db)) == len(audit_after_fire1)
    # (c) the schedule row still exists (NOT deleted)
    row = db.execute(
        "SELECT enabled, next_fire_at FROM schedules WHERE id = ?",
        (sid,)).fetchone()
    assert row is not None
    # the schedule itself is still on (the *source* is paused, not the schedule)
    assert row[0] == 1
    # (d) next_fire_at NOT advanced — still the due value we just set, so the
    #     schedule fires when the source is re-enabled
    assert row[1] == fire2_due

    db.close()


# 2 — re-enabling fires the still-due schedule (companion proof) -------------

def test_reenabled_source_fires_still_due_schedule(
        qdrant, tmp_path, fs_dir, monkeypatch):
    """Companion to FR-7: after a paused fire leaves the schedule still due,
    re-enabling the source makes the SAME due schedule fire on the next tick.
    Proves the pause is a no-op on the schedule, not a deletion or advance."""
    monkeypatch.setattr(
        "qdrant_client.QdrantClient",
        lambda *a, **kw: qdrant,
    )
    monkeypatch.setattr(
        "digital_twins.ingest.embedding.load_embedder",
        lambda model, device: _StubModel(),
    )

    cfg = _cfg(tmp_path, fs_dir)
    db = connect(tmp_path / "state")
    migrate(db)
    now = _now()
    sid = _due_schedule(db, now)

    # Fire 1: enabled -> fires (writes points + audit, advances the schedule).
    r1 = loop.serve_once_tick(db, cfg)
    assert sid in r1["fired"]

    # Pause + re-assert due, then fire 2 skipped.
    cfg_paused = copy.deepcopy(cfg)
    cfg_paused["sources"]["fs"]["enabled"] = False
    _make_due_again(db, sid, now)
    r2 = loop.serve_once_tick(db, cfg_paused)
    assert sid in r2["skipped"]
    # schedule row intact and still due
    assert db.execute(
        "SELECT enabled, next_fire_at FROM schedules WHERE id = ?",
        (sid,)).fetchone()[0] == 1

    # Re-enable: the still-due schedule now fires.
    r3 = loop.serve_once_tick(db, cfg)
    assert sid in r3["fired"]
    assert r3["skipped"] == []

    db.close()
