"""Resumable-run integration test (T014, 002 US4, 001 FR-5).

A run interrupted at ~20 of 50 items can be resumed by re-running the same
pipeline invocation. The 001 high-water marks + deterministic point IDs
guarantee the remaining 30 items are ingested without duplicating the first
20: final point count == 50 (not 70).

"Kill at ~20" is simulated with ``max_items=20`` for the first run, then a
restart with ``max_items=50`` (no effective cap) for the second run.

The test pins the 001 guarantee for 002's serve path: a schedule fire that is
interrupted (SIGKILL / power loss / crash) resumes cleanly on the next fire.
"""

import os
import time

import pytest

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.state.models import get_highwater

N_ITEMS = 50
PHASE1_CAP = 20  # "kill at ~20": first run processes only 20 of 50
PHASE2_CAP = N_ITEMS  # restart: no effective cap, process the rest


def _cfg(tmp_path, fs_dir):
    """Config: fs source only, state in tmp_path, deterministic embedding."""
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
    """Deterministic 384-dim vectors (one per input text)."""
    return [[0.5] * 384 for _ in texts]


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _point_ids(qdrant):
    """All point IDs in the collection (for the duplicate-check)."""
    ids = []
    offset = 0
    while True:
        batch, next_offset = qdrant.scroll(
            QDRANT_COLLECTION,
            limit=100,
            offset=offset,
            with_payload=False,
        )
        ids.extend(p.id for p in batch)
        if next_offset is None:
            break
        offset = next_offset
    return ids


@pytest.fixture
def fs_dir(tmp_path):
    """50 fixture files, zero-padded names (sort order == index order),
    each with distinct content and a strictly increasing mtime so the
    high-water cursor cleanly separates 'first 20' from 'remaining 30'."""
    d = tmp_path / "files"
    d.mkdir()
    for i in range(N_ITEMS):
        name = f"{i:03d}.txt"
        path = d / name
        path.write_text(f"item {i:03d} payload", encoding="utf-8")
        # strictly increasing mtime: 1 hour apart, deterministic
        mtime = time.time() + i * 3600
        os.utime(path, (mtime, mtime))
    return d


def test_resumable_run_no_duplicates(qdrant, tmp_path, fs_dir):
    """Run 1 (max_items=20) + Run 2 (max_items=50) => exactly 50 points."""
    cfg = _cfg(tmp_path, fs_dir)
    db = connect(tmp_path / "state")
    migrate(db)

    # --- Phase 1: "kill at ~20" — process only 20 of 50 items ---
    s1 = run_pipeline(cfg, db, qdrant, _embedder,
                      max_items=PHASE1_CAP, trigger="manual",
                      scheduled_by="system")
    assert s1.status == "ok"
    assert s1.counts == {"fs": PHASE1_CAP}, (
        f"phase 1 should process exactly {PHASE1_CAP} items, got {s1.counts}")
    assert s1.points == PHASE1_CAP, (
        f"phase 1 should write {PHASE1_CAP} points, got {s1.points}")
    assert _point_count(qdrant) == PHASE1_CAP, (
        f"after phase 1, qdrant should have {PHASE1_CAP} points, "
        f"got {_point_count(qdrant)}")
    # high-water cursor advanced: the fs source has a row for each of the
    # 20 processed items
    assert get_highwater(db, "fs", "000.txt") is not None, (
        "high-water for the first file should be set after phase 1")
    assert get_highwater(db, "fs", "019.txt") is not None, (
        "high-water for the 20th file should be set after phase 1")
    # the 21st file should NOT have a high-water row yet
    assert get_highwater(db, "fs", "020.txt") is None, (
        "high-water for the 21st file should NOT be set after phase 1")

    # --- Phase 2: restart — continue from the high-water mark ---
    s2 = run_pipeline(cfg, db, qdrant, _embedder,
                      max_items=PHASE2_CAP, trigger="manual",
                      scheduled_by="system")
    assert s2.status == "ok"
    remaining = N_ITEMS - PHASE1_CAP
    assert s2.counts == {"fs": remaining}, (
        f"phase 2 should process the remaining {remaining} items, "
        f"got {s2.counts}")
    assert s2.points == remaining, (
        f"phase 2 should write {remaining} new points, got {s2.points}")

    # --- Assert: exactly 50 unique points total (NOT 70) ---
    total = _point_count(qdrant)
    assert total == N_ITEMS, (
        f"after both runs, qdrant should have exactly {N_ITEMS} points "
        f"(20 from phase 1 + 30 from phase 2), got {total} — "
        f"duplication or gap detected")

    # --- Assert: no duplicate point IDs ---
    ids = _point_ids(qdrant)
    unique = set(ids)
    assert len(unique) == N_ITEMS, (
        f"expected {N_ITEMS} unique point IDs, got {len(unique)} "
        f"(duplicates: {len(ids) - len(unique)})")

    # --- Assert: high-water advanced to cover all 50 items ---
    assert get_highwater(db, "fs", "049.txt") is not None, (
        "high-water for the last file should be set after phase 2")

    # --- Assert: two audit rows, both ok ---
    rows = db.execute(
        "SELECT status FROM audit_runs ORDER BY started_at").fetchall()
    assert len(rows) == 2, f"expected 2 audit rows, got {len(rows)}"
    assert all(r[0] == "ok" for r in rows), (
        f"expected both runs to be 'ok', got {[r[0] for r in rows]}")

    db.close()
