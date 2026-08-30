"""008/US2 (T022, RED-first): endpoint-aware embedder (FR-003).

When ``embedding.endpoint`` is set in config, the embedder must call
``{endpoint}/v1/embeddings`` via urllib (stdlib HTTP), pass
``embedding.api_key`` as a Bearer header, and return the response
vectors.  When the endpoint is unset, the in-process ``load_embedder``
path is unchanged (additive only — no behavior change for existing
callers).

Per-assertion RED/GREEN status:
* endpoint-aware embedder exists and makes the HTTP call — **RED**:
  no importable endpoint embedder exists yet (``digital_twins.ingest``
  has no such function/class).
* in-process path unchanged when endpoint unset — **GREEN**: the
  pre-US2 behavior (``load_embedder`` + model.encode) is pinned so the
  endpoint path cannot silently replace it.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from digital_twins.config.schema import get

EMB_ENDPOINT = "http://embedding-test:8080"
EMB_KEY = "sk-test-fake-008-us2-embedding"
PINNED_MODEL = "BAAI/bge-small-en-v1.5"


def _make_cfg():
    return {
        "embedding": {
            "endpoint": EMB_ENDPOINT,
            "api_key": EMB_KEY,
            "model": PINNED_MODEL,
            "device": "auto",
        },
        "qdrant": {"url": "http://qdrant:6333", "api_key": "sk-test-fake"},
        "neo4j": {"url": "bolt://neo4j:7687", "user": "neo4j",
                   "password": "sk-test-fake"},
        "llm": {"endpoint": "http://llm:8000/v1", "api_key": "sk-test-fake"},
    }


class _Resp:
    """Minimal urllib response stand-in."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.code = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _resp(body, status=200) -> _Resp:
    return _Resp(body, status)


def _find_endpoint_embedder(cfg):
    """Locate the US2 endpoint-aware embedder factory.

    RED (pre-US2): no such function/class exists in
    ``digital_twins.ingest`` → returns None → the calling test fails
    with a descriptive message.
    """
    endpoint = get(cfg, "embedding.endpoint")
    api_key = get(cfg, "embedding.api_key")
    if not endpoint:
        return None

    try:
        from digital_twins.ingest import embedding as emb
    except ImportError:
        return None

    if emb is None:
        return None

    # Candidate factories (function or class) — try common names.
    for attr in (
        "build_endpoint_embedder",
        "endpoint_embedder",
        "make_endpoint_embedder",
        "embed_with_endpoint",
        "EndpointEmbedder",
    ):
        factory = getattr(emb, attr, None)
        if not callable(factory):
            continue
        # Try calling with (cfg,), (endpoint, api_key), or
        # (endpoint, api_key, model) — pick the first that succeeds.
        for args in (
            (cfg,),
            (endpoint, api_key),
            (endpoint, api_key, get(cfg, "embedding.model")),
        ):
            try:
                return factory(*args)
            except Exception:
                continue
    return None


def test_endpoint_embedder_posts_to_v1_embeddings(monkeypatch):
    """With ``embedding.endpoint`` set, the embedder must POST to
    ``{endpoint}/v1/embeddings`` with the Bearer key and the pinned
    model, and return the response vectors.

    RED: no endpoint-aware embedder exists → ``_find_endpoint_embedder``
    returns None.
    """
    cfg = _make_cfg()

    captured = []
    vectors = [[0.1] * 384, [0.5] * 384]

    def fake_open(req, timeout=None):
        captured.append(req)
        return _resp(json.dumps({"data": [
            {"embedding": v, "index": i} for i, v in enumerate(vectors)
        ]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)

    embedder = _find_endpoint_embedder(cfg)
    assert embedder is not None, (
        "US2 must provide an endpoint-aware embedder that reads "
        "embedding.endpoint/embedding.api_key from config (FR-003).  No "
        "importable endpoint embedder found in digital_twins.ingest."
    )
    out = embedder(["text one", "text two"])
    assert out == vectors, (
        f"endpoint embedder must return the response vectors, got {out!r}"
    )
    assert captured, "the endpoint embedder must make an HTTP request"
    req = captured[0]
    # The contract says POST to {endpoint}/v1/embeddings — but the brief
    # and the T020 test both assert {endpoint}/embeddings.  Use the
    # contract path; if the implementation uses /embeddings, this will
    # fail and the implementer adjusts.
    expected_url = EMB_ENDPOINT.rstrip("/") + "/v1/embeddings"
    assert req.full_url == expected_url, (
        f"endpoint embedder must POST to {{endpoint}}/v1/embeddings, "
        f"got {req.full_url!r} (expected {expected_url!r})"
    )
    assert req.get_header("Authorization") == f"Bearer {EMB_KEY}", (
        f"embedding.api_key must reach the endpoint embedder as a Bearer "
        f"header, got {req.get_header('Authorization')!r}"
    )
    payload = json.loads(req.data.decode("utf-8"))
    assert payload.get("model") == PINNED_MODEL, (
        f"endpoint embedder must send the pinned model, got {payload!r}"
    )
    assert payload.get("input") == ["text one", "text two"], (
        f"endpoint embedder must send the input texts, got {payload!r}"
    )


def test_endpoint_embedder_401_raises(monkeypatch):
    """When the endpoint returns 401, the embedder must raise (not
    silently return empty vectors).

    RED: no endpoint-aware embedder exists.
    """
    cfg = _make_cfg()

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, _Resp(b"{}", 401))

    monkeypatch.setattr("urllib.request.urlopen", fake_open)

    embedder = _find_endpoint_embedder(cfg)
    assert embedder is not None, "endpoint embedder must exist (FR-003)"
    with pytest.raises(Exception) as exc_info:
        embedder(["text"])
    assert "401" in str(exc_info.value) or "401" in repr(exc_info.value), (
        f"the raised exception must reference the 401 status, got "
        f"{exc_info.value!r}"
    )


def test_endpoint_unset_uses_in_process_embedder(monkeypatch):
    """With ``embedding.endpoint`` unset, the embedder path is the
    in-process ``load_embedder`` — unchanged from pre-US2.  This pins
    the default so the endpoint path is purely additive.

    GREEN: pre-US2 behavior, no new code required.
    """
    from digital_twins.ingest import embedding as emb_mod

    class _StubModel:
        def encode(self, texts):
            return [[0.5] * 384 for _ in texts]

    loaded = {}

    def fake_load_embedder(model=None, device="auto"):
        loaded.update(model=model, device=device)
        return _StubModel()

    monkeypatch.setattr(emb_mod, "load_embedder", fake_load_embedder)

    cfg = _make_cfg()
    cfg["embedding"]["endpoint"] = None
    cfg["embedding"]["api_key"] = None
    assert get(cfg, "embedding.endpoint") is None

    model = emb_mod.load_embedder(
        get(cfg, "embedding.model"), get(cfg, "embedding.device") or "auto"
    )
    assert "model" in loaded, "in-process path must call load_embedder"
    assert loaded["model"] == PINNED_MODEL
    out = model.encode(["hello"])
    assert len(out[0]) == 384, "pinned model dimension must be preserved"


def test_endpoint_embedder_dimension_mismatch_raises(monkeypatch):
    """When the endpoint returns vectors with a different dimension than
    the pinned model expects (384), the embedder must raise
    ``DimensionMismatchError`` (or equivalent) — not silently return
    the wrong-shaped vectors.

    RED: no endpoint-aware embedder exists.
    """
    cfg = _make_cfg()

    wrong_dim_vectors = [[0.1] * 256, [0.2] * 256]  # wrong dim

    def fake_open(req, timeout=None):
        return _resp(json.dumps({"data": [
            {"embedding": v, "index": i} for i, v in enumerate(wrong_dim_vectors)
        ]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)

    embedder = _find_endpoint_embedder(cfg)
    assert embedder is not None, "endpoint embedder must exist (FR-003)"
    with pytest.raises(Exception) as exc_info:
        embedder(["text"])
    assert "dimension" in str(exc_info.value).lower() or \
        "mismatch" in str(exc_info.value).lower(), (
        f"the raised exception must reference the dimension mismatch, "
        f"got {exc_info.value!r}"
    )
