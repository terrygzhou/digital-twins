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
