"""Pinned embedding loader + device knob (T012)."""

import sys
import types

import pytest

from digital_twins.config import SchemaError, load
from digital_twins.ingest.embedding import (
    PINNED_MODELS,
    load_embedder,
    model_dimension,
    resolve_device,
)


def test_pinned_model_dimension():
    assert model_dimension("BAAI/bge-small-en-v1.5") == 384
    assert PINNED_MODELS == {"BAAI/bge-small-en-v1.5": 384}


def test_unpinned_model_rejected():
    with pytest.raises(SchemaError, match="pin"):
        model_dimension("some/other-model")


def test_resolve_device_explicit():
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("cuda") == "cuda"


def test_resolve_device_auto_prefers_cuda(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert resolve_device("auto") == "cuda"


def test_resolve_device_auto_falls_back_to_cpu(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert resolve_device("auto") == "cpu"


def test_load_embedder_forwards_model_and_device(monkeypatch):
    calls = {}
    fake = types.ModuleType("sentence_transformers")

    class FakeST:
        def __init__(self, model, device=None, **kw):
            calls["model"] = model
            calls["device"] = device
            calls["local_files_only"] = kw.get("local_files_only")

    fake.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    load_embedder(model="BAAI/bge-small-en-v1.5", device="cpu")
    assert calls["model"] == "BAAI/bge-small-en-v1.5"
    assert calls["device"] == "cpu"
    # offline-safe: the loader must never probe HuggingFace (reads the
    # local cache only) — regression guard for the HF-DNS failure mode.
    assert calls["local_files_only"] is True


def test_device_out_of_range_rejected_by_schema(tmp_path):
    (tmp_path / "kb.yml").write_text(
        "embedding:\n  device: tpu\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="device"):
        load(cwd=tmp_path, config_dir=tmp_path)


# --- 0.10.0 fail-fast paths (local-embedding extra) -----------------------

def test_load_embedder_missing_extra_raises_with_remediation(monkeypatch):
    """When sentence_transformers is not installed, load_embedder raises
    LocalEmbedderError whose message names the extra and the endpoint
    alternative (no raw ImportError traceback)."""
    import digital_twins.ingest.embedding as emb

    def _fake_import(name, *a, **kw):
        raise ImportError("No module named 'sentence_transformers'")
    # Block the sentence_transformers import path.
    import builtins, sys
    real_import = builtins.__import__
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    def _guarded_import(name, *a, **kw):
        if name == "sentence_transformers":
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", _guarded_import, raising=False)

    with pytest.raises(emb.LocalEmbedderError, match="local-embedding"):
        emb.load_embedder()


def test_load_embedder_download_failure_raises_with_remediation(monkeypatch):
    """When SentenceTransformer construction fails (e.g. offline HF
    download), load_embedder raises LocalEmbedderError naming the
    endpoint alternative — not a raw traceback."""
    import digital_twins.ingest.embedding as emb

    class _FakeST:
        def __init__(self, model, device="cpu"):
            raise OSError("HTTPConnectionPool: Connection broken — offline")
    monkeypatch.setattr(
        emb, "SentenceTransformer", _FakeST, raising=False)
    # Also make the module-level import succeed by injecting a fake module.
    import sys, types
    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    with pytest.raises(emb.LocalEmbedderError, match="KB_EMBEDDING__ENDPOINT"):
        emb.load_embedder()
