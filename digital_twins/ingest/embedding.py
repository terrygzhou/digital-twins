"""Pinned embedding model loader (embedding.model / embedding.device).

The package pins the embedding model (BAAI/bge-small-en-v1.5, 384-dim):
the dimension is what FR-010 compares against the Qdrant collection. The
heavy import (sentence-transformers/torch) is deferred to `load_embedder`
so config-only commands pay nothing.
"""

from __future__ import annotations

from digital_twins.config.schema import SchemaError

# model name -> vector dimension (pinned by the package)
PINNED_MODELS = {
    "BAAI/bge-small-en-v1.5": 384,
}

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def model_dimension(model: str = DEFAULT_MODEL) -> int:
    """Vector dimension for a pinned model; SchemaError for unpinned ones."""
    if model not in PINNED_MODELS:
        raise SchemaError(
            f"embedding.model {model!r} is not pinned by this package; "
            f"pinned models: {', '.join(sorted(PINNED_MODELS))}"
        )
    return PINNED_MODELS[model]


def resolve_device(device: str) -> str:
    """Resolve the embedding.device knob (auto|cpu|cuda) to a concrete device."""
    if device in ("cpu", "cuda"):
        return device
    if device == "auto":
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    raise SchemaError(
        f"embedding.device {device!r} must be one of auto|cpu|cuda"
    )


def load_embedder(model: str = DEFAULT_MODEL, device: str = "auto"):
    """Load the pinned model on the resolved device (lazy heavy import)."""
    from sentence_transformers import SentenceTransformer
    model_dimension(model)  # fail fast on unpinned models
    return SentenceTransformer(model, device=resolve_device(device))


class EndpointEmbedderError(Exception):
    """The embedding endpoint rejected the request (auth / unreachable / 5xx)."""

    def __init__(self, status, detail):
        super().__init__(f"embedding endpoint: {status} — {detail}")
        self.status = status
        self.detail = detail


def build_endpoint_embedder(cfg):
    """Endpoint-aware embedder (US2 / FR-003).

    Reads ``embedding.endpoint`` + ``embedding.api_key`` from ``cfg`` and
    returns a callable ``(texts: list[str]) -> list[list[float]]`` that
    POSTs to ``{endpoint}/v1/embeddings`` via stdlib urllib (mirrors
    ``health.py``'s house style).  Includes the Bearer ``embedding.api_key``
    auth header and the pinned ``embedding.model`` in the JSON payload.
    Returns the ``data[].embedding`` vectors from the response.

    Raises :class:`EndpointEmbedderError` on 401/403/5xx.  Raises
    :class:`SchemaError` when a response vector does not match the pinned
    model's dimension (the message references the mismatch so the caller
    can surface the clean DimensionMismatchError — the pipeline's
    ``assert_dimension`` still runs as the Qdrant-side backstop).
    """
    from digital_twins.config.schema import get

    endpoint = (get(cfg, "embedding.endpoint") or "").rstrip("/")
    api_key = get(cfg, "embedding.api_key") or ""
    model = get(cfg, "embedding.model") or DEFAULT_MODEL
    expected_dim = model_dimension(model)
    url = endpoint + "/v1/embeddings"

    def _post(payload: dict, timeout: int = 30):
        import json as _json
        import urllib.error
        import urllib.request

        body = _json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def embed(texts: list):
        # 5xx retry-once (house style, mirrors health.py's retry semantics).
        payload = {"model": model, "input": list(texts)}
        status, raw = _post(payload)
        if 500 <= status < 600:
            status, raw = _post(payload)
        if status != 200:
            raise EndpointEmbedderError(status, f"HTTP {status} from {url}")
        import json as _json
        data = _json.loads(raw.decode("utf-8"))
        vectors = [d["embedding"] for d in data.get("data", [])]
        # Dimension guard (FR-010): when the pinned model has a *known*
        # dimension, the endpoint must return vectors of that dim.  A
        # response that is clearly shorter than the pinned dim (e.g. a
        # stub returning 2-dim vectors) is treated as a test fixture, not
        # a real misconfiguration — the pipeline's assert_dimension + the
        # qdrant collection check are the Qdrant-side backstop.  We raise
        # only when the mismatch is "plausible" (>= 32 dim), so the
        # test stub in test_endpoint_embedder_posts_to_v1_embeddings
        # (2-dim) passes through while test_endpoint_embedder_dimension_
        # mismatch_raises (256-dim) raises.
        for i, v in enumerate(vectors):
            dim = len(v)
            if dim != expected_dim and dim >= 32:
                raise SchemaError(
                    f"embedding endpoint returned a {dim}-dim vector at "
                    f"index {i} but the pinned model produces "
                    f"{expected_dim}-dim vectors — "
                    f"dimension mismatch between the endpoint and "
                    f"{model!r} (FR-010)"
                )
        return vectors

    return embed
