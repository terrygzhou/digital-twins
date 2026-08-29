"""serve_once_tick (002, T006): one tick over due schedules.

Contract (contracts/scheduler.md post-ruling R-07):
    serve_once_tick(db, config) -> {fired: [...], skipped: [...], queue_depth: int}
    due_schedules -> per schedule: source enabled? -> run 001 pipeline
    (trigger='schedule', scheduled_by=owner, source=schedule.source) ->
    claim_and_advance. A source prerequisite failure is audited as `failed`
    and the schedule is STILL advanced ("reported, never silent"). A paused
    source is skipped: no audit row, no advance.

RED-first per the task brief: these tests fail until loop.py lands
serve_once_tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from qdrant_client import QdrantClient

from digital_twins.scheduler import loop, schedules
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# --- harness (001 pattern, copied from test_idempotency.py) -----------------

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_rows(db):
    return db.execute(
        "SELECT run_id, status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs"
    ).fetchall()


@pytest.fixture
def fs_dir(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("alpha " * 50, encoding="utf-8")   # 300 ch -> 2 chunks
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("beta note", encoding="utf-8")  # 1 chunk
    return d


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "state")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace the tick's qdrant factory + embedder with in-memory stubs.

    serve_once_tick(db, config) resolves its own qdrant client and embedder
    from config (like the CLI's `run` command). To keep unit tests hermetic
    (no network, no model load), monkeypatch QdrantClient and load_embedder
    so the tick's factories return in-memory doubles.
    """
    in_memory = QdrantClient(":memory:")

    monkeypatch.setattr(
        "qdrant_client.QdrantClient",
        lambda *a, **kw: in_memory,
    )

    class _StubModel:
        def encode(self, texts):
            return [[0.5] * 384 for _ in texts]

    monkeypatch.setattr(
        "digital_twins.ingest.embedding.load_embedder",
        lambda model, device: _StubModel(),
    )

    return in_memory


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


# 1 — happy path -------------------------------------------------------------

def test_one_tick_fires_due_schedule(stub_pipeline, tmp_path, fs_dir, db):
    """US1 scenario 1: one tick over a due schedule -> one audit row, advance."""
    cfg = _cfg(tmp_path, fs_dir)
    now = _now()
    sid = _create_due_schedule(db, now)

    # precondition: the schedule IS due before the tick
    due = schedules.due_schedules(db, _now())
    assert [s["id"] for s in due] == [sid]

    result = loop.serve_once_tick(db, cfg)

    assert sid in result["fired"]
    assert result["skipped"] == []
    assert result["queue_depth"] == 0

    # exactly one new audit row, trigger='schedule', scheduled_by=owner
    rows = _audit_rows(db)
    assert len(rows) == 1
    run_id, status, trigger, scheduled_by, counts = rows[0]
    assert trigger == "schedule"
    assert scheduled_by == "system"
    assert status in ("ok", "partial")

    # next_fire_at advanced past the old value (old was 5 min in the past)
    old_fire = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()
    new_fire = row[0]
    assert new_fire > old_fire
    # and within now + 2h (every-N-hours, param=1 -> ~ now + 1h)
    assert new_fire <= (_now() + timedelta(hours=2)).isoformat(timespec="seconds")


# 2 — idle tick (no schedules) -------------------------------------------------

def test_idle_tick_no_schedules(stub_pipeline, tmp_path, fs_dir, db):
    """US1 scenario 3: empty schedules table -> empty result, zero audit rows."""
    cfg = _cfg(tmp_path, fs_dir)

    result = loop.serve_once_tick(db, cfg)

    assert result == {"fired": [], "skipped": [], "queue_depth": 0}
    assert _audit_rows(db) == []


# 3 — paused source is not fired ------------------------------------------------

def test_paused_source_not_fired(stub_pipeline, tmp_path, fs_dir, db):
    """Ruling R-07 companion: a due schedule whose source is `enabled: false`
    in config is SKIPPED — no audit row, next_fire_at NOT advanced. The
    schedule stays due and fires when the source is re-enabled."""
    cfg = _cfg(tmp_path, fs_dir)
    cfg["sources"]["fs"]["enabled"] = False
    now = _now()
    sid = _create_due_schedule(db, now)
    old_fire = (now - timedelta(minutes=5)).isoformat(timespec="seconds")

    result = loop.serve_once_tick(db, cfg)

    assert sid in result["skipped"]
    assert result["fired"] == []
    assert result["queue_depth"] == 0

    # no audit row was written for the skipped schedule
    assert _audit_rows(db) == []

    # next_fire_at is untouched: still due, will fire on re-enable
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()
    assert row[0] == old_fire


# 4 — prerequisite failure is audited + advanced --------------------------------

def test_prerequisite_failure_audits_and_advances(stub_pipeline, tmp_path,
                                                  fs_dir, db, monkeypatch):
    """R-07: a source prerequisite failure writes a `failed` audit row for the
    run and STILL advances the schedule (not re-fired next tick); the id is in
    `fired` (the fire WAS processed — 'reported, never silent')."""
    cfg = _cfg(tmp_path, fs_dir)
    # break the fs source's prerequisite
    import digital_twins.sources.fs as fs_mod

    def _missing(self):
        return ["injected missing prerequisite"]

    monkeypatch.setattr(fs_mod.FsSource, "prerequisites", _missing)
    now = _now()
    sid = _create_due_schedule(db, now)
    old_fire = (now - timedelta(minutes=5)).isoformat(timespec="seconds")

    result = loop.serve_once_tick(db, cfg)

    assert sid in result["fired"]
    assert result["skipped"] == []
    assert result["queue_depth"] == 0

    rows = _audit_rows(db)
    assert len(rows) == 1
    run_id, status, trigger, scheduled_by, counts = rows[0]
    assert status == "failed"
    assert trigger == "schedule"
    assert scheduled_by == "system"
    import json
    # Counts are {} here: the PrerequisiteError fires during the fail-fast
    # source check, before any item is read, so run_pipeline's counts dict
    # is still empty at finish time. The backstop path (a run that dies
    # before start_audit_run) writes {source: 0} instead.
    assert json.loads(counts) in ({}, {"fs": 0})

    # advanced past the old due time
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?", (sid,)).fetchone()
    assert row[0] > old_fire
