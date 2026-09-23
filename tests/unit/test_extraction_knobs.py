"""Config knob tests for extraction.* knobs (s4-entity-extraction).

Verifies the new knobs are registered in the KNOBS registry and that
validate() fills them in with the correct defaults.
"""
from __future__ import annotations

import pytest

from digital_twins.config.knobs import KNOBS
from digital_twins.config.schema import DEFAULTS, get, validate


def test_extraction_knobs_in_registry():
    assert "extraction.enabled" in KNOBS
    assert "extraction.max_text_chars" in KNOBS
    assert "extraction.prompt_version" in KNOBS


def test_extraction_knobs_in_defaults():
    assert "extraction.enabled" in DEFAULTS
    assert DEFAULTS["extraction.enabled"] is False
    assert DEFAULTS["extraction.max_text_chars"] == 12000
    assert DEFAULTS["extraction.prompt_version"] == ""


def test_extraction_knobs_validate_fills_defaults():
    """validate() fills extraction.* with defaults even when omitted."""
    cfg = {
        "state_dir": "/tmp/test",
        "config_dir": "/tmp/test/config",
        "qdrant": {"url": "https://q:6333"},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 800, "overlap": 100},
        "sources": {},
    }
    validated = validate(cfg)
    assert validated["extraction"]["enabled"] is False
    assert validated["extraction"]["max_text_chars"] == 12000
    assert validated["extraction"]["prompt_version"] == ""


def test_extraction_knobs_coerce_types():
    """extraction.enabled coerces "true"/"false" strings; max_text_chars
    coerces int strings."""
    cfg = {
        "state_dir": "/tmp/test",
        "config_dir": "/tmp/test/config",
        "qdrant": {"url": "https://q:6333"},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 800, "overlap": 100},
        "extraction": {"enabled": "true", "max_text_chars": "5000"},
        "sources": {},
    }
    validated = validate(cfg)
    assert validated["extraction"]["enabled"] is True
    assert validated["extraction"]["max_text_chars"] == 5000
