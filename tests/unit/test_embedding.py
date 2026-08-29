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
        def __init__(self, model, device=None):
            calls["model"] = model
            calls["device"] = device

    fake.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    load_embedder(model="BAAI/bge-small-en-v1.5", device="cpu")
    assert calls == {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"}


def test_device_out_of_range_rejected_by_schema(tmp_path):
    (tmp_path / "kb.yml").write_text(
        "embedding:\n  device: tpu\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="device"):
        load(cwd=tmp_path, config_dir=tmp_path)
