"""008/US1 (T003, RED-first): embedding.endpoint + embedding.api_key knobs.

The reference compose already passes KB_EMBEDDING__ENDPOINT into the app
container and the schema rejects unknown keys — so the knob must exist in
DEFAULTS, KNOBS, and the shipped example files (T027 guard).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from digital_twins.config.schema import DEFAULTS, SchemaError, validate
from digital_twins.config.knobs import KNOBS

_HERE = Path(__file__).resolve().parents[2]


def test_defaults_declare_the_optional_knobs():
    assert "embedding.endpoint" in DEFAULTS
    assert DEFAULTS["embedding.endpoint"] is None
    assert "embedding.api_key" in DEFAULTS
    assert DEFAULTS["embedding.api_key"] is None


def test_knobs_registry_entries():
    for dotted, env in (("embedding.endpoint", "KB_EMBEDDING__ENDPOINT"),
                        ("embedding.api_key", "KB_EMBEDDING__API_KEY")):
        entry = KNOBS.get(dotted)
        assert entry is not None, f"{dotted} missing from KNOBS"
        assert entry["type"] == "str"
        assert entry["default"] is None
        assert entry["env"] == env


def test_validate_accepts_embedding_endpoint():
    cfg = validate({
        "embedding": {"endpoint": "http://embed.example:9000/v1",
                      "api_key": "sek"}})
    assert cfg["embedding"]["endpoint"] == "http://embed.example:9000/v1"
    assert cfg["embedding"]["api_key"] == "sek"


def test_unset_knobs_default_to_none():
    cfg = validate({})
    assert cfg["embedding"]["endpoint"] is None
    assert cfg["embedding"]["api_key"] is None


def test_unknown_embedding_sub_still_rejected():
    with pytest.raises(SchemaError, match="unknown config key"):
        validate({"embedding": {"endpointx": "x"}})


def test_env_mapping(tmp_path):
    from digital_twins.config.loader import load
    cfg = load(cwd=tmp_path, env={"KB_EMBEDDING__ENDPOINT": "http://e:1/v1",
                                  "KB_EMBEDDING__API_KEY": "k1"})
    assert cfg["embedding"]["endpoint"] == "http://e:1/v1"
    assert cfg["embedding"]["api_key"] == "k1"


def test_documented_in_example_files():
    example = (_HERE / "config.example.yml").read_text()
    assert "(env: KB_EMBEDDING__ENDPOINT)" in example
    assert "(env: KB_EMBEDDING__API_KEY)" in example
    env_example = (_HERE / ".env.example").read_text()
    assert "#KB_EMBEDDING__ENDPOINT=" in env_example
    assert "#KB_EMBEDDING__API_KEY=" in env_example
