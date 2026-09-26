"""Dimension guard in the run pipeline (T033, S2-mismatch, NFR-2).

`run` must fail fast when the existing Qdrant collection's vector dimension
does not match the pinned embedding model, raising DimensionMismatchError
instead of upserting into the wrong collection.

Also hosts the preflight-optional-deps Qdrant-only mode test
(`run_pipeline(neo4j=None)` completes a Qdrant write without touching any
graph write helper).
"""

import logging

import pytest

from digital_twins.ingest import pipeline
from digital_twins.ingest.pipeline import (
    DimensionMismatchError,
    _ensure_collection,
    assert_dimension,
    run_pipeline,
)

from qdrant_client import models as qm

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.state.db import connect


# --- fakes -----------------------------------------------------------------

class FakeQdrantClient:
    """Qdrant client fake recording created collections and their dim."""

    def __init__(self, existing_dim=None):
        self.existing_dim = existing_dim
        self.created = []

    def collection_exists(self, name):
        return name == QDRANT_COLLECTION and self.existing_dim is not None

    def create_collection(self, name, vectors_config):
        self.created.append((name, vectors_config.size))
        self.existing_dim = vectors_config.size

    def get_collection(self, name):
        assert name == QDRANT_COLLECTION
        return type("Info", (), {
            "config": type("Config", (), {
                "params": type("Params", (), {
                    "vectors": qm.VectorParams(
                        size=self.existing_dim, distance=qm.Distance.COSINE)})
            })()
        })()

    def upsert(self, *a, **kw):
        pass


def _trivial_source():
    class S:
        class capability:
            prefix = "trivial://"

        def prerequisites(self):
            return []

        def read(self, since=None):
            return iter(())

        def close(self):
            pass

    return S()


def _patch_source(monkeypatch):
    monkeypatch.setattr(
        pipeline, "build_source",
        lambda name, entry: _trivial_source())


def _cfg():
    return {
        "state_dir": "/tmp/x",
        "config_dir": "/tmp/y",
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": {
            "trivial": {"enabled": True},
            "fs": {"enabled": False},
        },
    }


# --- _ensure_collection (create-only) ----------------------------------------

def test_ensure_collection_creates_when_missing():
    client = FakeQdrantClient(existing_dim=None)
    _ensure_collection(client, 384)
    assert client.created == [(QDRANT_COLLECTION, 384)]


def test_ensure_collection_noop_when_collection_exists():
    """_ensure_collection does NOT re-check dim (assert_dimension does)."""
    client = FakeQdrantClient(existing_dim=384)
    _ensure_collection(client, 384)
    assert client.created == []


# --- assert_dimension (the guard) -------------------------------------------

def test_assert_dimension_passes_when_match():
    client = FakeQdrantClient(existing_dim=384)
    assert_dimension(client, 384)
    assert client.created == []  # no create side-effect


def test_assert_dimension_raises_on_mismatch():
    client = FakeQdrantClient(existing_dim=1536)
    with pytest.raises(DimensionMismatchError) as excinfo:
        assert_dimension(client, 384)
    err = excinfo.value
    assert err.collection == QDRANT_COLLECTION
    assert err.actual == 1536
    assert err.expected == 384
    msg = str(err)
    assert "1536" in msg and "384" in msg
    # remediation hint present
    assert "recreate" in msg or "re-embed" in msg


def test_assert_dimension_noop_when_collection_missing():
    client = FakeQdrantClient(existing_dim=None)
    assert_dimension(client, 384)  # no-op, no create
    assert client.created == []


# --- run_pipeline integration ------------------------------------------------

def test_run_pipeline_dimension_mismatch_raises(tmp_path, monkeypatch):
    """When qdrant has a 1536-dim collection, run_pipeline fails with
    DimensionMismatchError before upserting. The audit row is failed."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = connect(state_dir)
    from digital_twins.state import migrations
    migrations.migrate(conn)

    client = FakeQdrantClient(existing_dim=1536)
    _patch_source(monkeypatch)

    with pytest.raises(DimensionMismatchError):
        run_pipeline(cfg := _cfg(), conn, client,
                     embedder=lambda texts: [])

    # no upsert was attempted (FakeQdrantClient.upsert records nothing)
    rows = conn.execute("SELECT status FROM audit_runs").fetchall()
    assert rows and rows[-1][0] == "failed"
    conn.close()


def test_run_pipeline_dry_run_skips_dimension_check(tmp_path, monkeypatch):
    """dry_run=True must not resolve the qdrant factory at all."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = connect(state_dir)
    from digital_twins.state import migrations
    migrations.migrate(conn)

    def explode():
        raise AssertionError(
            "qdrant factory should not be called on dry_run")

    _patch_source(monkeypatch)
    summary = run_pipeline(_cfg(), conn, explode,
                           embedder=None, dry_run=True)
    assert summary.status == "ok"
    conn.close()


def test_run_pipeline_matching_dim_proceeds(tmp_path, monkeypatch):
    """Collection dim matches pinned dim -> no exception, run succeeds."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = connect(state_dir)
    from digital_twins.state import migrations
    migrations.migrate(conn)

    client = FakeQdrantClient(existing_dim=384)
    _patch_source(monkeypatch)
    summary = run_pipeline(_cfg(), conn, client,
                           embedder=lambda texts: [])
    assert summary.status == "ok"
    assert summary.points == 0
    conn.close()


# --- preflight-optional-deps: Qdrant-only mode (neo4j=None) -------------------

class _ItemSource:
    """Fake source yielding one real IngestItem so a Qdrant upsert happens."""

    class capability:
        prefix = "item://"

    def __init__(self):
        from digital_twins.sources.base import IngestItem
        self._item = IngestItem(key="i1", content="hello world",
                                 ts="2025-01-01T00:00:00Z")

    def prerequisites(self):
        return []

    def read(self, since=None):
        return iter([self._item])

    def close(self):
        pass


def _patch_item_source(monkeypatch):
    monkeypatch.setattr(
        pipeline, "build_source",
        lambda name, entry: _ItemSource())


def test_run_pipeline_neo4j_none_completes_qdrant_write(
        tmp_path, caplog, monkeypatch):
    """preflight-optional-deps 2.1: with neo4j=None the pipeline skips the
    graph write helpers (the helpers' monkeypatched sentinels raise if
    ever called) and completes the Qdrant write without raising — the
    exact Qdrant-only scenario from the spec: 'WHEN neo4j.url /
    neo4j.user / neo4j.password are unset and qdrant is healthy, THEN
    preflight returns without raising, the pipeline skips graph writes,
    and the Qdrant write completes'."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = connect(state_dir)
    from digital_twins.state import migrations
    migrations.migrate(conn)

    upserted = []

    class _Client(FakeQdrantClient):
        def upsert(self, *a, **kw):
            upserted.append((len(kw.get("points") or []),
                             kw.get("wait")))

    client = _Client(existing_dim=384)
    _patch_item_source(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("neo4j=None must not call graph write helpers")

    monkeypatch.setattr(pipeline, "_upsert_graph", _boom)
    monkeypatch.setattr(pipeline, "_ensure_graph_schema", _boom)
    monkeypatch.setattr(pipeline, "_run_extraction", _boom)
    monkeypatch.setattr(pipeline, "_execute_cypher", _boom)

    with caplog.at_level(logging.INFO, logger="digital_twins.ingest.pipeline"):
        summary = run_pipeline(_cfg(), conn, client,
                               embedder=lambda texts: [[0.1] * 384],
                               neo4j=None)

    assert summary.status == "ok"
    assert summary.points == 1
    assert upserted == [(1, True)]
    msgs = [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO]
    assert any("Qdrant-only mode" in m and "Neo4j not configured" in m
               for m in msgs)
    conn.close()


def test_run_pipeline_neo4j_driver_still_writes_graph(
        tmp_path, monkeypatch):
    """Sanity (not new behavior): with a session-API-shaped fake driver,
    the S4 graph write helpers are still invoked (session API surface
    post 78084f7 — _execute_cypher routes through .session())."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = connect(state_dir)
    from digital_twins.state import migrations
    migrations.migrate(conn)

    class _Client(FakeQdrantClient):
        def upsert(self, *a, **kw):
            pass

    client = _Client(existing_dim=384)
    _patch_item_source(monkeypatch)

    cypher_calls = []

    def _recording(neo4j, cypher, **params):
        cypher_calls.append(cypher)

    monkeypatch.setattr(pipeline, "_execute_cypher", _recording)

    class _S:
        def __enter__(self_):
            return self_
        def __exit__(self_, *a):
            return False
        def run(self_, query, **params):
            pass

    class _Driver:
        def session(self):
            return _S()

    summary = run_pipeline(_cfg(), conn, client,
                           embedder=lambda texts: [[0.1] * 384],
                           neo4j=_Driver())
    assert summary.status == "ok"
    assert any("SourceItem" in c for c in cypher_calls)
    assert any("CREATE INDEX" in c for c in cypher_calls)
    conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
