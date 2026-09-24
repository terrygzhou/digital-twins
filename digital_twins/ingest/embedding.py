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


def model_dimension(model: str = DEFAULT_MODEL,
                    optional: bool = False) -> int:
    """Vector dimension for a pinned model; SchemaError for unpinned ones.

    ``optional=True`` (external-endpoint use): an unpinned model name is
    accepted — the remote endpoint owns its model and its vectors, so the
    package cannot validate the name.  Dimension validation degrades to
    "known dims enforce, unknown dims pass through".
    """
    if model in PINNED_MODELS:
        return PINNED_MODELS[model]
    if optional:
        return 0
    raise SchemaError(
        f"embedding.model {model!r} is not pinned by this package; "
        f"pinned models: {', '.join(sorted(PINNED_MODELS))}"
    )


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


class LocalEmbedderError(Exception):
    """In-process embedding is unavailable (extra not installed or model
    download failed). The message carries the remediation line."""


def load_embedder(model: str = DEFAULT_MODEL, device: str = "auto"):
    """Load the pinned model on the resolved device (lazy heavy import).

    Raises LocalEmbedderError with a remediation line when
    sentence-transformers is not installed (the ``local-embedding``
    extra) or the pinned model cannot be loaded/downloaded.
    """
    model_dimension(model)  # fail fast on unpinned models
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise LocalEmbedderError(
            "in-process embedding needs the 'local-embedding' extra: "
            "pip install 'digital-twins[local-embedding]' — or set "
            "embedding.endpoint (env KB_EMBEDDING__ENDPOINT) to an "
            "OpenAI-compatible embedding endpoint"
        ) from exc
    try:
        return SentenceTransformer(model, device=resolve_device(device))
    except Exception as exc:
        raise LocalEmbedderError(
            f"could not load pinned embedding model {model!r} ({exc}); "
            "ensure network access for the model download, or set "
            "embedding.endpoint (env KB_EMBEDDING__ENDPOINT) to an "
            "OpenAI-compatible embedding endpoint"
        ) from exc


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
    auth header and the ``embedding.model`` name in the JSON payload.  A
    custom (non-pinned) model name is accepted — the endpoint owns its
    model; the dimension guard only enforces when the pinned model is a
    *known* dimension.  Returns the ``data[].embedding`` vectors from the
    response.

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
    # The endpoint owns its model: a custom (non-pinned) model name is
    # legal here — the dimension guard below only enforces when the
    # pinned model is a *known* dimension (a remote endpoint returning
    # vectors of another model's shape still fails the Qdrant upsert
    # backstop).
    expected_dim = model_dimension(model, optional=True)
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
        # dimension, the endpoint must return vectors of that dim — strict:
        # any mismatch fails fast here, not later at the Qdrant upsert.
        for i, v in enumerate(vectors):
            dim = len(v)
            if expected_dim and dim != expected_dim:
                raise SchemaError(
                    f"embedding endpoint returned a {dim}-dim vector at "
                    f"index {i} but {model!r} produces "
                    f"{expected_dim}-dim vectors — "
                    f"dimension mismatch between the endpoint and "
                    f"{model!r} (FR-010)"
                )
        return vectors

    return embed
