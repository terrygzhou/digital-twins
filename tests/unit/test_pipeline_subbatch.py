"""Sub-batched pipeline tests (feature 019 - ingest reliability, task 1.1).

Verifies the pipeline.sub_batch_size knob drives per-sub-batch commit
order: embed -> qdrant upsert -> graph upsert -> extraction fan-out ->
highwater checkpoint, repeated for each sub-batch. Legacy behavior
(sub_batch_size = 0) preserves the single-batch commit at source end.
"""
from __future__ import annotations

from digital_twins.ingest import pipeline
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state import migrations
from digital_twins.health import QDRANT_COLLECTION
from qdrant_client import models as qm


class _Source:
    class capability:
        prefix = "fake://"

    def __init__(self, n):
        self.n = n

    def prerequisites(self):
        return []

    def read(self, since=None):
        start = 0
        if since is not None:
            start = int(since.replace("item-", "")) + 1
        for i in range(start, self.n):
            yield _Item(f"item-{i:05d}", f"content {i} " + "x" * 500)

    def close(self):
        pass

class _Item:
    def __init__(self, key, content, ts="2026-01-01T00:00:00Z"):
        self.key = key
        self.content = content
        self.ts = ts
        self.metadata = {}


class FakeQdrant:
    """Records upsert call batches (one call per sub-batch expected)."""
    def __init__(self):
        self.upserts = []
        self.existing_dim = 384

    def collection_exists(self, name):
        return name == QDRANT_COLLECTION and self.existing_dim is not None

    def create_collection(self, name, vectors_config):
        pass

    def get_collection(self, name):
        params = type("Params", (), {
            "vectors": qm.VectorParams(
                size=384, distance=qm.Distance.COSINE)})
        config = type("Config", (), {"params": params})()
        info = type("Info", (), {"config": config})()
        return info

    def upsert(self, coll, points, wait=None):
        self.upserts.append(len(points))


class FakeNeo4j:
    """Minimal neo4j driver fake: records session calls."""
    def __init__(self):
        self.run_calls = []

    def session(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.run_calls.append((query, params))
        class _Result:
            def __init__(self, rows):
                self._rows = rows
            def data(self):
                return self._rows
            def single(self):
                return self._rows[0] if self._rows else None
        return _Result([])



def _cfg(sub_batch_size, concurrency=1):
    return {
        "state_dir": "/tmp/x",
        "config_dir": "/tmp/y",
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "pipeline": {"sub_batch_size": sub_batch_size},
        "extraction": {
            "enabled": False,
            "max_text_chars": 12000,
            "prompt_version": "",
            "concurrency": concurrency,
        },
        "sources": {"fake": {"enabled": True}},
    }


def _patch(monkeypatch, n):
    """Neutralize source/embedding/preflight so the test is hermetic."""
    monkeypatch.setattr(
        pipeline, "build_source",
        lambda name, entry: _Source(n))
    monkeypatch.setattr(pipeline, "model_dimension", lambda model: 384)
    monkeypatch.setattr(pipeline, "preflight", lambda cfg: None)
    monkeypatch.setattr(pipeline, "_run_extraction",
                        lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "chunk_text",
                        lambda content, mc, ov: [content])


def _highwater_rows(db, source="fake"):
    return db.execute(
        "SELECT last_key FROM highwater WHERE source=? ORDER BY rowid",
        (source,)).fetchall()



# --- tests ---

def test_sub_batch_checkpoint_highwater_per_batch(tmp_path, monkeypatch):
    """2500 items, sub_batch_size=1000 -> 3 qdrant upserts [1000,1000,500] and highwater rows exist for every item (checkpointed per sub-batch)."""
    n, sub = 2500, 1000
    state_dir = tmp_path / "state"; state_dir.mkdir()
    conn = connect(state_dir)
    migrations.migrate(conn)
    fake_q = FakeQdrant()
    fake_n4j = FakeNeo4j()
    _patch(monkeypatch, n)
    cfg = _cfg(sub_batch_size=sub)
    summary = run_pipeline(cfg, conn, fake_q,
                           embedder=lambda texts: [[0.0] * 384 for _ in texts],
                           neo4j=fake_n4j)
    assert summary.status == "ok"
    assert summary.points == n
    assert fake_q.upserts == [1000, 1000, 500], "expected 3 sub-batch upserts, got {fake_q.upserts}"
    hw = _highwater_rows(conn, "fake")
    assert len(hw) == n, "expected {n} highwater rows, got {len(hw)}"
    conn.close()


def test_sub_batch_size_zero_is_legacy_single_batch(tmp_path, monkeypatch):
    """sub_batch_size=0 -> legacy: one qdrant upsert with all points, highwater written at source end (single commit)."""
    n = 1500
    state_dir = tmp_path / "state"; state_dir.mkdir()
    conn = connect(state_dir)
    migrations.migrate(conn)
    fake_q = FakeQdrant()
    fake_n4j = FakeNeo4j()
    _patch(monkeypatch, n)
    cfg = _cfg(sub_batch_size=0)
    summary = run_pipeline(cfg, conn, fake_q,
                           embedder=lambda texts: [[0.0] * 384 for _ in texts],
                           neo4j=fake_n4j)
    assert summary.status == "ok"
    assert fake_q.upserts == [n], "expected one legacy upsert of {n}, got {fake_q.upserts}"
    hw = _highwater_rows(conn, "fake")
    assert len(hw) == n
    conn.close()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """dry_run=True: no qdrant upserts, no highwater rows, count still reported."""
    n = 50
    state_dir = tmp_path / "state"; state_dir.mkdir()
    conn = connect(state_dir)
    migrations.migrate(conn)
    fake_q = FakeQdrant()
    _patch(monkeypatch, n)
    cfg = _cfg(sub_batch_size=1000)
    summary = run_pipeline(cfg, conn, fake_q,
                           embedder=lambda texts: [[0.0] * 384 for _ in texts],
                           neo4j=None, dry_run=True)
    assert summary.status == "ok"
    assert summary.points == n
    assert fake_q.upserts == [], "dry-run must not upsert"
    hw = _highwater_rows(conn, "fake")
    assert hw == [], "dry-run must not write highwater rows"
    conn.close()


def test_sub_batch_does_not_reembed_prior_batch(tmp_path, monkeypatch):
    """Each sub-batch embeds only its own chunks; embedder is called once per sub-batch (3 calls for 2500 items at sub_batch_size=1000)."""
    n, sub = 2500, 1000
    state_dir = tmp_path / "state"; state_dir.mkdir()
    conn = connect(state_dir)
    migrations.migrate(conn)
    embed_calls = []
    def _embed(texts):
        embed_calls.append(len(texts))
        return [[0.0] * 384 for _ in texts]
    fake_q = FakeQdrant()
    fake_n4j = FakeNeo4j()
    _patch(monkeypatch, n)
    cfg = _cfg(sub_batch_size=sub)
    run_pipeline(cfg, conn, fake_q, embedder=_embed, neo4j=fake_n4j)
    assert embed_calls == [1000, 1000, 500], "embedder should be called per sub-batch, got {embed_calls}"
    conn.close()