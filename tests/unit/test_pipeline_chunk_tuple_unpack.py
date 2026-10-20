"""Regression: the embedder receives the chunk *text* (index 3), not the
trailing ``n_chunks`` int (index 4).

Context (docs/verification-2026-07-23-e2e-fs-ingest.md):

Chunk tuples are 5-tuples ``(point_id, chunk_index, item, text, n_chunks)``.
When the ``n_chunks`` payload field was appended, the embedding call kept the
"grab the last element" idiom::

    vectors = embedder([text for *_x, text in chunks])

which binds ``text`` to the *last* element (``n_chunks``, an int) and silently
fed ints to the embedder, raising ``ValueError: Unsupported input type: int``
on every live run. The fix addresses index 3 explicitly. This test guards the
public interface so a future unpack change (e.g. adding a 6th tuple element)
cannot silently rebind the wrong position.
"""

import pytest

from digital_twins.ingest import pipeline
from digital_twins.ingest.chunking import chunk_text
from digital_twins.ingest.pipeline import run_pipeline

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.state.db import connect


# --- fakes -----------------------------------------------------------------


class FakeQdrantClient:
    """Records upserted points and returns the pinned dim for a collection."""

    def __init__(self):
        self.created = []
        self.dim = 384

    def collection_exists(self, name):
        return True

    def create_collection(self, name, vectors_config):
        self.created.append((name, vectors_config.size))

    def get_collection(self, name):
        import qdrant_client.models as qm
        return type("Info", (), {
            "config": type("Config", (), {
                "params": type("Params", (), {
                    "vectors": qm.VectorParams(
                        size=self.dim, distance=qm.Distance.COSINE)})
            })()
        })()

    def upsert(self, *a, **kw):
        pass


def _text_source():
    """Fake source yielding one item that chunks into multiple parts."""

    class S:
        class capability:
            prefix = "text://"

        def prerequisites(self):
            return []

        def read(self, since=None):
            from digital_twins.sources.base import IngestItem
            # Long enough to produce >1 chunk under max_chars=50, overlap=10.
            yield IngestItem(
                key="doc/one",
                content=("word" + " ") * 200,
                ts="2025-01-01T00:00:00Z",
                metadata={"path": "doc/one"})

        def close(self):
            pass

    return S()


def _patch_text_source(monkeypatch):
    monkeypatch.setattr(pipeline, "build_source",
                        lambda name, entry: _text_source())


def _cfg():
    return {
        "state_dir": "/tmp/x",
        "config_dir": "/tmp/y",
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 50, "overlap": 10},
        "sources": {"text": {"enabled": True}},
    }


# --- direct: index 3 is text, index 4 is int ------------------------------


def test_chunk_tuple_index3_is_text_index4_is_int():
    """Ground the regression: for a multi-chunk item, tuple[3] is the str
    text and tuple[4] is the int n_chunks (the two positions the bug
    confused)."""
    texts = chunk_text(("word" + " ") * 200, 50, 10)
    assert len(texts) > 1
    n = len(texts)
    chunks = [(f"pid-{i}", i, _item(), t, n) for i, t in enumerate(texts)]
    for c in chunks:
        assert isinstance(c[3], str), "index 3 must be the chunk text (str)"
        assert isinstance(c[4], int), "index 4 must be n_chunks (int)"
        # The pre-fix idiom grabbed the *last* element — pin that it is the
        # int, not the text, so a silent rebind is caught.
        assert c[-1] == n
        assert c[3] not in (None,)


def _item():
    from digital_twins.sources.base import IngestItem
    return IngestItem(key="doc/one", content="x",
                      ts="2025-01-01T00:00:00Z")


# --- public interface: the embedder must receive texts ---------------------


def test_embedder_receives_text_not_n_chunks(tmp_path, monkeypatch):
    """Regression (pipeline.py:241): through the public run_pipeline path,
    the embedder is invoked with the list of chunk texts (index 3) — all
    strings — never the trailing n_chunks ints. A future tuple change that
    rebinds the last element would feed ints here and fail this assert."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = connect(state_dir)
    from digital_twins.state import migrations
    migrations.migrate(conn)

    client = FakeQdrantClient()
    _patch_text_source(monkeypatch)

    received = []

    def embedder(texts):
        received.append(list(texts))
        # Return 384-dim vectors so the upsert path proceeds.
        return [[0.1] * 384 for _ in texts]

    summary = run_pipeline(_cfg(), conn, client,
                           embedder=embedder, neo4j=None)

    assert summary.status == "ok"
    assert len(received) == 1
    texts = received[0]
    assert len(texts) > 1, "item must produce multiple chunks"
    for t in texts:
        assert isinstance(t, str), (
            "embedder must receive chunk text (str) at tuple index 3, "
            "not the trailing n_chunks int")
    conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
