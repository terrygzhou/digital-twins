"""Per-user channel-override resolution in the scheduler tick (5.2).

The scheduler's per-run source resolution order is
``user_override > env > kb.local.yml > kb.yml > defaults``. The global
config handed to the tick is already env-resolved (the config loader
applies env > kb.local.yml > kb.yml > defaults); the tick then layers the
schedule owner's per-user overrides on top via ``merge_user_config``
(``digital_twins/scheduler/loop.py``, per due schedule), and re-checks
the enabled gate against the *merged* config so a user's
``enabled: false`` override suppresses the source for that user's runs
only (BR-11.4.2 / SC-003).

These tests drive the **real** ``serve_once_tick`` against a real
in-memory Qdrant, a real fs source, and a real state db (the
``test_serve_once_dedup.py`` harness pattern) and assert the observable
behaviour of the per-user layer of that order:

1. **per-user disable is isolated**: a user's ``enabled: false``
   override for source X suppresses that source's runs *for that user
   only*; another user's runs of the same source proceed normally in a
   later tick.
2. **user_override > env**: a user's ``max_items`` override beats the
   env-resolved global value.  The global value here is produced by a
   real ``load()`` with a ``KB_`` env var set (the env layer genuinely
   resolves it), so the "global" value is the env-resolved value, not a
   hard-coded dict.
3. **no-op without overrides**: when the owner has no override rows the
   merge is a no-op (the merged config equals the global config).

The harness runs in-sandbox (in-memory Qdrant + stub embedder +
health-check stubs), so no environmental fallback is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from qdrant_client import QdrantClient

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.scheduler import loop, schedules
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.user_config import merge_user_config, set_override


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fs_dir_with(tmp_path, n_files: int):
    """A test fs source dir with ``n_files`` distinct single-chunk files.

    Each file is a short note (< chunking.max_chars) -> exactly 1 chunk
    per file, so the ingested item count == the file count until a cap
    bites.
    """
    d = tmp_path / "files"
    d.mkdir()
    for i in range(n_files):
        (d / f"f{i:03d}.txt").write_text(f"note number {i} alpha beta",
                                         encoding="utf-8")
    return d


def _cfg(tmp_path, fs_dir, cap: int = 200):
    """A config dict with a single enabled ``fs`` source over ``fs_dir``,
    ``max_items=cap``. Every other source is disabled (mirrors
    ``test_serve_once_dedup.py``)."""
    names = ("hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs")
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in names}
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


def _create_due_schedule(db, now: datetime, *, owner: str, source: str):
    """Create a schedule and force next_fire_at into the past (due NOW)."""
    sched = schedules.create_schedule(db, owner, source, "every-N-hours",
                                     param=1, fire_time="03:00", now=now)
    past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
        (past, sched["id"]),
    )
    db.commit()
    return sched["id"]


@pytest.fixture
def shared_qdrant(monkeypatch):
    """In-memory Qdrant shared by the tick's trigger path, plus stubbed
    embedder and health checks (no network, no model load, no live
    neo4j/llm/embedding probes)."""
    import digital_twins.ingest.embedding as emb
    import digital_twins.health as health

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
    # The preflight gate probes neo4j/llm/embedding services; stub the
    # three probes so the tick runs hermetically (the qdrant probe uses
    # the in-memory client from the constructor monkeypatch above).
    for name in ("check_neo4j", "check_llm", "check_embedding"):
        monkeypatch.setattr(
            health, name,
            lambda cfg, n=name: health.HealthResult(n, True, "test stub"),
        )
    return in_memory


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "state")
    migrate(conn)
    yield conn
    conn.close()


def _audit_rows(db, owner):
    """The owner's audit rows as ``[{per_source_counts, status}]``."""
    rows = db.execute(
        "SELECT per_source_counts, status FROM audit_runs WHERE scheduled_by=?",
        (owner,),
    ).fetchall()
    return [{"counts": json.loads(r[0]), "status": r[1]} for r in rows]


def _point_count(qdrant) -> int:
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _owner_stamps(qdrant):
    """The meta.owner value of every ingested point."""
    if not qdrant.collection_exists(QDRANT_COLLECTION):
        return []
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=1000)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    return [p.payload.get("meta", {}).get("owner") for p in points]


# 1 — per-user disable is isolated (BR-11.4.2 / SC-003) ---------------------

def test_user_enabled_false_suppresses_only_that_user(
        tmp_path, db, shared_qdrant):
    """A user's ``enabled: false`` override for the fs source suppresses
    the fs source for THAT user's runs only; another user's runs of the
    same source proceed normally (BR-11.4.2 / SC-003).

    The operator config leaves fs enabled (a kb.local.yml-level
    decision). Alice's per-user override disables it for her; Bob has no
    override, so his runs use the operator config.
    """
    fs_dir = _fs_dir_with(tmp_path, 2)
    cfg = _cfg(tmp_path, fs_dir)
    set_override(db, "alice", "fs", "enabled", "false")

    # Tick 1: only alice's due schedule -> skipped (her override), no fire.
    now = _now()
    alice_sid = _create_due_schedule(db, now, owner="alice", source="fs")
    alice_old_fire = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    r1 = loop.serve_once_tick(db, cfg)
    assert r1 == {"fired": [], "skipped": [alice_sid], "queue_depth": 0}, r1
    # No audit row for alice (skip semantics: nothing ran).
    assert _audit_rows(db, "alice") == []
    # Alice's schedule is NOT advanced (still due; fires when her
    # override is removed).
    row = db.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?",
        (alice_sid,)).fetchone()
    assert row[0] == alice_old_fire, "skipped schedule must not advance"

    # Tick 2: add bob's due schedule -> bob FIRES normally; alice's
    # (still due) schedule remains skipped in the same tick.
    now2 = _now()
    bob_sid = _create_due_schedule(db, now2, owner="bob", source="fs")
    r2 = loop.serve_once_tick(db, cfg)
    assert bob_sid in r2["fired"], f"bob must fire: {r2}"
    assert alice_sid in r2["skipped"], f"alice must stay skipped: {r2}"

    # Bob's run ingested both files and stamped owner=bob; alice
    # contributed nothing.
    bob_rows = _audit_rows(db, "bob")
    assert len(bob_rows) == 1, f"expected 1 bob audit row: {bob_rows}"
    assert bob_rows[0]["counts"] == {"fs": 2}, bob_rows
    assert bob_rows[0]["status"] == "ok"
    assert _point_count(shared_qdrant) == 2
    assert _owner_stamps(shared_qdrant) == ["bob", "bob"]
    # The global config is still operator-enabled (SC-003: no mutation).
    assert cfg["sources"]["fs"]["enabled"] is True


# 2 — user_override > env ----------------------------------------------------

def test_user_max_items_beats_env_resolved_global(
        tmp_path, db, shared_qdrant):
    """A user's ``max_items`` override beats the env-resolved global value.

    The global value is produced by a real ``load()`` with a ``KB_`` env
    var set (the env layer), so the global config genuinely IS the
    env-resolved value: ``KB_SOURCES__FS__MAX_ITEMS=60``. Alice overrides
    ``max_items=50``; with 60 files in the dir her run ingests 50 (her
    cap beat the env value). Bob (no override) runs after the cursor has
    advanced: only the 10 not-yet-ingested files are new, and his
    effective cap is still the env value (60) -> he ingests all 10.
    (If bob's run had been affected by alice's override, the per-user
    layer would have leaked across owners — SC-003.)
    """
    fs_dir = _fs_dir_with(tmp_path, 60)

    # Env-resolved global config: KB_SOURCES__FS__MAX_ITEMS=60 wins over
    # the built-in default (200).  KB_QDRANT__URL is set so the env layer
    # configures qdrant (the preflight check requires a non-empty url);
    # the real client is the in-memory double from the shared_qdrant
    # fixture (monkeypatched at the constructor).
    import os
    env = dict(os.environ)
    env["KB_SOURCES__FS__MAX_ITEMS"] = "60"
    env["KB_QDRANT__URL"] = "https://q.example:6333"
    from digital_twins.config import load
    cfg = load(cwd=tmp_path, config_dir=tmp_path, env=env)
    assert cfg["sources"]["fs"]["max_items"] == 60, (
        f"test setup: env-resolved global fs cap must be 60, "
        f"got {cfg['sources']['fs']['max_items']}")
    # The loader resolves state_dir/config_dir from env defaults; point
    # them at the test sandbox (mirrors the _cfg helper's contract).
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["config_dir"] = str(tmp_path / "config")
    cfg["sources"]["fs"]["enabled"] = True
    cfg["sources"]["fs"]["extra"] = {"dir": str(fs_dir)}

    # Alice caps fs at 50 (beats the env-resolved 60).
    set_override(db, "alice", "fs", "max_items", 50)

    # Alice fires first: the cursor starts empty, so her run sees all 60
    # files and the per-user cap (50) bites -> 50 items.
    now = _now()
    alice_sid = _create_due_schedule(db, now, owner="alice", source="fs")
    r1 = loop.serve_once_tick(db, cfg)
    assert alice_sid in r1["fired"], r1
    alice_rows = _audit_rows(db, "alice")
    assert len(alice_rows) == 1, alice_rows
    assert alice_rows[0]["counts"] == {"fs": 50}, (
        f"alice's cap (50) must beat the env cap (60); "
        f"got {alice_rows[0]}")
    assert _point_count(shared_qdrant) == 50

    # Bob's run after the cursor advanced: 10 new files, env cap (60)
    # not binding -> 10. His effective cap is NOT alice's 50.
    bob_sid = _create_due_schedule(db, _now(), owner="bob", source="fs")
    r2 = loop.serve_once_tick(db, cfg)
    assert bob_sid in r2["fired"], r2
    bob_rows = _audit_rows(db, "bob")
    assert len(bob_rows) == 1, bob_rows
    assert bob_rows[0]["counts"] == {"fs": 10}, (
        f"bob (no override) ingests the 10 new files under the env cap "
        f"(60); got {bob_rows}")
    assert _point_count(shared_qdrant) == 60

    # The global config the tick used is still the env value, unmutated
    # by alice's override (SC-003 at the serve level).
    assert cfg["sources"]["fs"]["max_items"] == 60, (
        "global config must stay env-resolved (60); the merge is a copy")


# 3 — no-op without overrides -------------------------------------------------

def test_merge_noop_when_no_overrides(db, tmp_path):
    """When the owner has no override rows, the merge is a no-op: the
    merged config equals the global config (no override layer added)."""
    cfg = _cfg(tmp_path, _fs_dir_with(tmp_path, 1))
    merged = merge_user_config(cfg, db, "nobody@example.com")
    assert merged == cfg, "no overrides -> merged must equal global (no-op)"
    # And it is a fresh dict (never the same object), so a later
    # mutation of the merge cannot corrupt the global (SC-003 isolation).
    assert merged is not cfg


if __name__ == "__main__":  # pragma: no cover - manual smoke
    raise SystemExit(pytest.main([__file__, "-q"]))
