"""007 Phase 3 (T010/T011): ``kb_search`` tool body (007-R1, SC-001/SC-002/SC-007).

Constitution III (Test-First): the RED tests in this file are committed and
failing against the 004 BR-10 stub lambda (which returns
``not_implemented_yet``) before the GREEN implementation (T011) lands.

T010 RED (this file) asserts:
  (a) owner-scoped filter — a fake Qdrant client (seeded via the dispatch
      module's client seam) captures ``query_points`` args: the
      ``query_filter`` is exactly
      ``Filter(must=[FieldCondition(key="owner_tag",
      match=MatchValue(value=owner_tag_for(caller)))])`` on
      ``health.QDRANT_COLLECTION``, with ``limit`` clamped and
      ``with_payload=True``;
  (b) ``{"ok": True, "results": [{score, source_url, text, source,
      chunk_index}], "count": N}`` from fake rows, sorted descending,
      ``count = len(results)``;
  (c) blank/missing query → ``bad_request "query must be a non-empty
      string"`` (exact 006 message) and **no** Qdrant call;
  (d) limit default 5 / cap 100 / non-int → default;
  (e) unconfigured/unreachable Qdrant → ``qdrant_unavailable`` + the exact
      006 remediation string;
  (f) broken embedder → ``embedding_unavailable`` (a distinct code, naming
      ``embedding.model``);
  (g) ``MCPContext(config=None)`` → ``config_not_loaded`` before any other
      work.

Embedding is faked at the dispatch module seam (never load the real model
in tests).
"""
from __future__ import annotations

import sqlite3

import pytest

from digital_twins.accounts import create_account, owner_tag_for
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.mcp.dispatch import dispatch
from digital_twins.mcp.registry import MCPContext
from digital_twins.state.db import state_db_path
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated state DB with one reader account (the caller)."""
    path = state_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    create_account(conn, "alice@example.com", "pw-alice")
    conn.execute(
        "UPDATE accounts SET role='reader' WHERE email='alice@example.com'")
    conn.commit()
    yield conn
    conn.close()


def _ctx(db, config=None, email="alice@example.com", role="reader",
         agent_kind="hermes"):
    return MCPContext(db, email, role, agent_kind, config=config)


# A minimal config that the body will read (qdrant.url + embedding.*).
_BASE_CFG = {
    "qdrant": {"url": "http://127.0.0.1:6333"},
    "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
}


# ---------------------------------------------------------------------------
# fake Qdrant client + fake embedder (seeded via the dispatch module seams)
# ---------------------------------------------------------------------------

class _FakeQueryPoint:
    """Mimics a qdrant_client query result row: .score + .payload."""
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


class _FakeQdrantClient:
    """Captures ``query_points`` args; returns canned rows.

    ``seeded`` distinguishes a usable client from a construction failure
    (the unconfigured / unreachable case raises at construction time in the
    real body).
    """
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query_points(self, collection, query=None, query_filter=None,
                     limit=None, with_payload=False, **kw):
        self.calls.append({
            "collection": collection,
            "query": query,
            "query_filter": query_filter,
            "limit": limit,
            "with_payload": with_payload,
        })
        return self.rows


class _FakeEmbedder:
    """Returns a fixed 384-dim vector (matching the pinned model)."""
    def __init__(self, dim=384):
        self.dim = dim
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        # A list of `texts` fake vectors (the body takes the first).
        return _FakeEmbeddingBatch([[0.1] * self.dim] * len(list(texts)))


class _FakeEmbeddingBatch:
    """Mimics a sentence-transformers encode() result (has .tolist())."""
    def __init__(self, vectors):
        self._vectors = vectors

    def tolist(self):
        return self._vectors


# ---------------------------------------------------------------------------
# (g) config=None → config_not_loaded (fail-closed, before any other work)
# ---------------------------------------------------------------------------

def test_kb_search_config_none_fails_closed(db):
    """MCPContext(config=None) → config_not_loaded, no Qdrant/embedding work."""
    import digital_twins.mcp.dispatch as dispatch_mod

    qdrant_calls = []
    embed_calls = []

    def fake_qdrant_client(cfg):
        qdrant_calls.append(cfg)
        return _FakeQdrantClient(rows=[])

    def fake_embed_query(cfg, text):
        embed_calls.append((cfg, text))
        return [0.1] * 384

    monkeypatched = _Monkeypatcher()
    monkeypatched.set(dispatch_mod, "_resolve_qdrant_client", fake_qdrant_client)
    monkeypatched.set(dispatch_mod, "_embed_query", fake_embed_query)
    try:
        result = dispatch(_ctx(db, config=None), "kb_search", {"query": "hi"})
    finally:
        monkeypatched.undo()

    assert result["ok"] is False
    assert result["error"]["code"] == "config_not_loaded"
    assert qdrant_calls == []   # no Qdrant work when config is None
    assert embed_calls == []    # no embedding work when config is None


# ---------------------------------------------------------------------------
# (c) blank/missing query → bad_request, no Qdrant call
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query_arg", [None, "", "   ", 123, ["q"]])
def test_kb_search_blank_or_missing_query_bad_request(db, query_arg, monkeypatch):
    """Blank/missing/non-str query → bad_request 'query must be a non-empty
    string', and NO Qdrant call (validation before any I/O)."""
    import digital_twins.mcp.dispatch as dispatch_mod

    qdrant_calls = []

    def fake_qdrant_client(cfg):
        qdrant_calls.append(cfg)
        return _FakeQdrantClient(rows=[])

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant_client, raising=False)
    result = dispatch(_ctx(db, config=dict(_BASE_CFG)), "kb_search",
                      {"query": query_arg})
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert result["error"]["message"] == "query must be a non-empty string"
    assert qdrant_calls == []  # no Qdrant call on validation failure


# ---------------------------------------------------------------------------
# (d) limit default 5 / cap 100 / non-int → default
# ---------------------------------------------------------------------------

def test_kb_search_limit_default_clamp_and_nonint(db, monkeypatch):
    """limit: default 5, capped at 100, non-int → 5.

    The fake Qdrant client captures the ``limit`` passed to query_points;
    we run three calls with limit omitted / 1000 / 'bogus' and assert the
    clamped values are 5 / 100 / 5.
    """
    import digital_twins.mcp.dispatch as dispatch_mod

    rows = [_FakeQueryPoint(0.9, {"source_url": "u", "text": "t",
                                  "source": "fs", "chunk_index": 0}),
            _FakeQueryPoint(0.5, {"source_url": "u2", "text": "t2",
                                  "source": "fs", "chunk_index": 1})]
    captured = []

    def fake_qdrant_client(cfg):
        client = _FakeQdrantClient(rows=rows)
        captured.append(client)
        return client

    def fake_embed_query(cfg, text):
        return [0.1] * 384

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant_client, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed_query,
                        raising=False)

    cfg = dict(_BASE_CFG)
    # default (omit limit)
    dispatch(_ctx(db, config=cfg), "kb_search", {"query": "hi"})
    # over the cap
    dispatch(_ctx(db, config=cfg), "kb_search", {"query": "hi", "limit": 1000})
    # non-int → default
    dispatch(_ctx(db, config=cfg), "kb_search", {"query": "hi", "limit": "x"})

    assert [c.calls[0]["limit"] for c in captured] == [5, 100, 5]
    # every call used with_payload=True and the right collection
    for c in captured:
        assert c.calls[0]["with_payload"] is True
        assert c.calls[0]["collection"] == QDRANT_COLLECTION


# ---------------------------------------------------------------------------
# (a) owner-scoped filter (the exact 006 Filter/FieldCondition/MatchValue)
# ---------------------------------------------------------------------------

def test_kb_search_owner_scoped_filter(db, monkeypatch):
    """query_points receives exactly
    Filter(must=[FieldCondition(key='owner_tag', match=MatchValue(value=
    owner_tag_for(caller)))]) on QDRANT_COLLECTION."""
    import digital_twins.mcp.dispatch as dispatch_mod
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    rows = []
    captured = []

    def fake_qdrant_client(cfg):
        client = _FakeQdrantClient(rows=rows)
        captured.append(client)
        return client

    def fake_embed_query(cfg, text):
        return [0.1] * 384

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant_client, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed_query,
                        raising=False)

    caller = "bob@example.com"
    dispatch(_ctx(db, config=dict(_BASE_CFG), email=caller), "kb_search",
             {"query": "hello"})

    expected_filter = Filter(must=[FieldCondition(
        key="owner_tag",
        match=MatchValue(value=owner_tag_for(caller)))])
    assert len(captured) == 1
    assert captured[0].calls[0]["query_filter"] == expected_filter
    assert captured[0].calls[0]["collection"] == QDRANT_COLLECTION


# ---------------------------------------------------------------------------
# (b) success shape: results sorted descending, count = len(results)
# ---------------------------------------------------------------------------

def test_kb_search_success_shape_sorted_desc(db, monkeypatch):
    """{"ok": True, "results": [{score, source_url, text, source,
    chunk_index}], "count": N} with rows sorted descending by score."""
    import digital_twins.mcp.dispatch as dispatch_mod

    rows = [
        _FakeQueryPoint(0.3, {"source_url": "low", "text": "low text",
                              "source": "fs", "chunk_index": 0}),
        _FakeQueryPoint(0.9, {"source_url": "high", "text": "high text",
                              "source": "pi", "chunk_index": 2}),
        _FakeQueryPoint(0.6, {"source_url": "mid", "text": "mid text",
                              "source": "dsh", "chunk_index": 1}),
    ]

    def fake_qdrant_client(cfg):
        return _FakeQdrantClient(rows=rows)

    def fake_embed_query(cfg, text):
        return [0.1] * 384

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant_client, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed_query,
                        raising=False)

    result = dispatch(_ctx(db, config=dict(_BASE_CFG)), "kb_search",
                      {"query": "find me"})

    assert result["ok"] is True
    assert result["count"] == len(result["results"]) == 3
    # descending score order
    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)
    assert scores == [0.9, 0.6, 0.3]
    # exact field set per row
    for r in result["results"]:
        assert set(r.keys()) == {"score", "source_url", "text", "source",
                                 "chunk_index"}
    # the highest-score row is first
    assert result["results"][0]["source_url"] == "high"


# ---------------------------------------------------------------------------
# (e) unconfigured/unreachable Qdrant → qdrant_unavailable + exact remediation
# ---------------------------------------------------------------------------

def test_kb_search_qdrant_unavailable(db, monkeypatch):
    """_resolve_qdrant_client raises → qdrant_unavailable + the exact 006
    remediation string."""
    import digital_twins.mcp.dispatch as dispatch_mod

    REMEDIATION = ("check qdrant.url (env: KB_QDRANT__URL) points at a live "
                   "Qdrant host:port, and that the collection exists")

    class _QdrantUnavailable(Exception):
        pass

    def fake_qdrant_client(cfg):
        raise _QdrantUnavailable("qdrant.url is not configured")

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant_client, raising=False)

    result = dispatch(_ctx(db, config=dict(_BASE_CFG)), "kb_search",
                      {"query": "hi"})
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "qdrant_unavailable"
    # The 006 remediation string must be present (remediation key, exact text).
    assert REMEDIATION in (err.get("remediation") or err.get("message") or "")


def test_kb_search_unconfigured_qdrant(db, monkeypatch):
    """A config with no qdrant.url → _resolve_qdrant_client must raise →
    qdrant_unavailable (the real helper's unconfigured path)."""
    import digital_twins.mcp.dispatch as dispatch_mod

    REMEDIATION = ("check qdrant.url (env: KB_QDRANT__URL) points at a live "
                   "Qdrant host:port, and that the collection exists")
    cfg = {"embedding": {"model": "BAAI/bge-small-en-v1.5",
                          "device": "cpu"}}  # no qdrant.url
    # Do NOT monkeypatch _resolve_qdrant_client: exercise the real helper's
    # unconfigured path (it must raise a QdrantUnavailable-class error).
    result = dispatch(_ctx(db, config=cfg), "kb_search", {"query": "hi"})
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "qdrant_unavailable"
    assert REMEDIATION in (err.get("remediation") or err.get("message") or "")


# ---------------------------------------------------------------------------
# (f) broken embedder → embedding_unavailable (distinct code, names
#      embedding.model)
# ---------------------------------------------------------------------------

def test_kb_search_embedding_unavailable(db, monkeypatch):
    """_embed_query raises → embedding_unavailable (a code distinct from
    qdrant_unavailable), naming embedding.model."""
    import digital_twins.mcp.dispatch as dispatch_mod

    def fake_qdrant_client(cfg):
        return _FakeQdrantClient(rows=[])

    def fake_embed_query(cfg, text):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(dispatch_mod, "_resolve_qdrant_client",
                        fake_qdrant_client, raising=False)
    monkeypatch.setattr(dispatch_mod, "_embed_query", fake_embed_query,
                        raising=False)

    result = dispatch(_ctx(db, config=dict(_BASE_CFG)), "kb_search",
                      {"query": "hi"})
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "embedding_unavailable"
    # distinct from qdrant_unavailable, and names embedding.model
    assert err["code"] != "qdrant_unavailable"
    blob = err.get("remediation") or err.get("message") or ""
    assert "embedding.model" in blob


# ---------------------------------------------------------------------------
# a small monkeypatch helper (mirrors the Phase 1 test style)
# ---------------------------------------------------------------------------

class _Monkeypatcher:
    """Minimal attribute-set/undo helper (avoids importing pytest's
    MonkeyPatch inline where the test is already param-free)."""
    def __init__(self):
        self._saved = []

    def set(self, module, name, value):
        import digital_twins.mcp.dispatch as dispatch_mod
        target = module if module is not None else dispatch_mod
        if hasattr(target, name):
            self._saved.append((target, name, getattr(target, name)))
        else:
            self._saved.append((target, name, None))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._saved):
            if old is None:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
            else:
                setattr(target, name, old)
        self._saved = []
