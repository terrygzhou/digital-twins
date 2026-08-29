"""Dimension-mismatch health check (T031, US4; S2-mismatch, NFR-2).

Pins the contract: when the Qdrant collection's vector dimension does not
match the pinned embedding model's dimension, `check_qdrant` is a hard
error with an actionable remediation hint — never a silent data loss.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from digital_twins import health
from digital_twins.config.schema import SchemaError
from digital_twins.ingest.embedding import model_dimension

PINNED = "BAAI/bge-small-en-v1.5"
PINNED_DIM = 384  # the dimension this package pins (FR-010)


def _cfg(**sections):
    return sections


def _install_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)


# --- qdrant fakes -----------------------------------------------------------

class _NoCollections:
    """Reachable Qdrant with no collections yet."""

    def __init__(self, url=None, api_key=None):
        pass

    def get_collections(self):
        return SimpleNamespace(collections=[])


def _dim_client(dim):
    """Qdrant fake whose personal_kb collection has `dim`-dim vectors."""

    class _DimClient:
        def __init__(self, url=None, api_key=None):
            pass

        def get_collections(self):
            return SimpleNamespace(
                collections=[SimpleNamespace(name=health.QDRANT_COLLECTION)])

        def get_collection(self, name):
            return SimpleNamespace(config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=dim))))
    return _DimClient


# --- tests ------------------------------------------------------------------

def test_dimension_mismatch_is_hard_error(monkeypatch):
    """pinned 384 vs collection 1536 -> ok=False, detail names both dims."""
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_dim_client(1536))
    r = health.check_qdrant(
        _cfg(qdrant={"url": "http://localhost:6333"},
             embedding={"model": PINNED}))
    assert r.endpoint == "qdrant"
    assert not r.ok
    assert "1536" in r.detail
    assert "384" in r.detail
    assert "dim" in r.detail.lower()  # both sides of the mismatch named


def test_dimension_mismatch_remediation_is_actionable(monkeypatch):
    """Remediation tells the operator what to do (recreate or re-embed)."""
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_dim_client(1536))
    r = health.check_qdrant(
        _cfg(qdrant={"url": "http://localhost:6333"},
             embedding={"model": PINNED}))
    assert not r.ok
    assert r.remediation
    assert "recreate" in r.remediation or "re-embed" in r.remediation


def test_matching_dimension_passes(monkeypatch):
    """collection 384 == pinned 384 -> ok=True."""
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_dim_client(384))
    r = health.check_qdrant(
        _cfg(qdrant={"url": "http://localhost:6333"},
             embedding={"model": PINNED}))
    assert r.ok
    assert "384" in r.detail


def test_missing_collection_is_creatable(monkeypatch):
    """No personal_kb yet -> ok=True; it will be created on first run."""
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_NoCollections)
    r = health.check_qdrant(_cfg(qdrant={"url": "http://localhost:6333"}))
    assert r.ok
    assert "will be created" in r.detail


def test_unpinned_model_raises_schema_error():
    """model_dimension rejects models the package does not pin."""
    with pytest.raises(SchemaError) as excinfo:
        model_dimension("not-a-pinned-model")
    assert "not pinned" in str(excinfo.value)
    assert PINNED in str(excinfo.value)  # the error names what is pinned


def test_pinned_model_dimension_is_384():
    """The check's 'expected' side of the comparison is stable at 384."""
    assert model_dimension(PINNED) == PINNED_DIM
