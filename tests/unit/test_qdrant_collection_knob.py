"""qdrant.collection knob (independence): the collection name is
user-configurable, not a host-coupled constant.

Covers:
- DEFAULTS + KNOBS registry declare the knob (T027 lockstep surfaces
  are asserted by test_knob_docs.py; this file covers behaviour).
- qdrant_collection(cfg) resolution: default, override, empty → default.
- check_qdrant uses the resolved name (message reflects the override).
"""
from __future__ import annotations

from digital_twins.config.schema import DEFAULTS
from digital_twins.config.knobs import KNOBS
from digital_twins.health import QDRANT_COLLECTION, check_qdrant, qdrant_collection


def test_defaults_declare_the_knob():
    assert DEFAULTS["qdrant.collection"] == QDRANT_COLLECTION


def test_knobs_registry_entry():
    entry = KNOBS.get("qdrant.collection")
    assert entry is not None
    assert entry["type"] == "str"
    assert entry["default"] == "personal_kb"
    assert entry["env"] == "KB_QDRANT__COLLECTION"


def test_resolve_defaults_when_unset():
    assert qdrant_collection({}) == QDRANT_COLLECTION


def test_resolve_uses_override():
    cfg = {"qdrant": {"collection": "my_own_collection"}}
    assert qdrant_collection(cfg) == "my_own_collection"


def test_resolve_empty_falls_back_to_default():
    cfg = {"qdrant": {"collection": ""}}
    assert qdrant_collection(cfg) == QDRANT_COLLECTION


def test_check_qdrant_message_uses_resolved_name(monkeypatch):
    """check_qdrant reports the configured collection, not the constant."""
    import types

    class _C:
        def __init__(self, name):
            self.name = name

    def _fake_get_collections():
        class _R:
            collections = [_C("personal_kb")]
        return _R()

    fake_client = types.SimpleNamespace(get_collections=_fake_get_collections)
    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient",
                        lambda *a, **k: fake_client)

    # point the knob at a name the (stub) host does NOT have → the
    # "will be created" message must name the override, not personal_kb.
    cfg = {"qdrant": {"url": "http://q:6333", "collection": "alt_coll"}}
    res = check_qdrant(cfg)
    assert res.ok is True
    assert "alt_coll" in res.detail
    assert "personal_kb" not in res.detail
