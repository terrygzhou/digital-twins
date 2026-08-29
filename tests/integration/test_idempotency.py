"""Idempotency invariant (T018, NFR-1): one point per item, not N.

The same content ingested via schedule, run --once, MCP or web UI must
yield exactly one point per chunk: deterministic point IDs + upserts,
reinforced by high-water marks.
"""

import pytest

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.state.models import get_highwater


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


def _embedder(texts):
    return [[0.5] * 384 for _ in texts]


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


@pytest.fixture
def fs_dir(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("alpha " * 50, encoding="utf-8")   # 300 ch -> 2 chunks
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("beta note", encoding="utf-8")  # 1 chunk
    return d


def test_reingest_same_content_one_point(qdrant, tmp_path, fs_dir):
    cfg = _cfg(tmp_path, fs_dir)
    db = connect(tmp_path / "state")
    migrate(db)

    s1 = run_pipeline(cfg, db, qdrant, _embedder)
    assert s1.points == 3
    assert s1.counts == {"fs": 2}

    # second run: high-water marks mean nothing new is read
    s2 = run_pipeline(cfg, db, qdrant, _embedder)
    assert s2.points == 0
    assert _point_count(qdrant) == 3

    # third run: cursors wiped, full re-read must still not duplicate
    db.execute("DELETE FROM highwater")
    db.commit()
    s3 = run_pipeline(cfg, db, qdrant, _embedder)
    assert s3.points == 3
    assert _point_count(qdrant) == 3

    # high-water marks advanced and persisted
    assert get_highwater(db, "fs", "a.txt") is not None
    assert get_highwater(db, "fs", "sub/b.txt") is not None

    # one audit row per run, all ok
    rows = db.execute("SELECT status FROM audit_runs").fetchall()
    assert len(rows) == 3
    assert all(r[0] == "ok" for r in rows)
    db.close()


def test_new_file_picked_up_on_next_run(qdrant, tmp_path, fs_dir):
    cfg = _cfg(tmp_path, fs_dir)
    db = connect(tmp_path / "state")
    migrate(db)

    s1 = run_pipeline(cfg, db, qdrant, _embedder)
    assert s1.points == 3

    import os
    import time
    c = fs_dir / "c.txt"
    c.write_text("gamma", encoding="utf-8")
    later = time.time() + 3600
    os.utime(c, (later, later))  # deterministic mtime strictly after the first run
    s2 = run_pipeline(cfg, db, qdrant, _embedder)
    assert s2.points == 1
    assert s2.counts == {"fs": 1}
    assert _point_count(qdrant) == 4
    db.close()
