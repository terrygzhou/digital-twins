"""Pipeline extraction hook tests (s4-entity-extraction, task 3).

Hermetic: monkeypatches ``digital_twins.ingest.entities._llm_request``
and uses ``StubNeo4j`` for the graph driver — no real LLM or Neo4j calls.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tests.conftest import StubNeo4j
from digital_twins.ingest import pipeline
from digital_twins.ingest.entities import PROMPT_VERSION


def _make_item(key: str, content: str, ts: str = "2026-01-01T00:00:00Z"):
    """Minimal item-shaped object (matches source.read() contract)."""
    item = MagicMock()
    item.key = key
    item.content = content
    item.ts = ts
    item.metadata = {"title": key, "tags": []}
    return item


def _extraction_cfg():
    return {
        "extraction": {
            "enabled": True,
            "max_text_chars": 12000,
            "prompt_version": "",
        },
        "llm": {"endpoint": "http://llm:9000", "model": "test", "api_key": "k"},
    }


def test_extraction_skipped_when_gate_off(monkeypatch):
    """extraction.enabled=false (default) → _run_extraction is a no-op."""
    fake_driver = StubNeo4j()
    llm_calls = []

    def fake_llm(messages, cfg=None):
        llm_calls.append(messages)
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(
        pipeline.entities, "_llm_request", fake_llm)

    cfg = {"extraction": {"enabled": False}}
    items = [_make_item("a", "text a")]
    item_hash = {"a": "hash-a"}

    pipeline._run_extraction(cfg, fake_driver, items, item_hash, "r1", "fs")

    assert not llm_calls, "LLM should NOT be called when gate is off"
    assert not fake_driver.run_calls, \
        "No graph writes expected when gate is off"


def test_extraction_runs_when_gate_on_and_driver_present(monkeypatch):
    fake_driver = StubNeo4j()
    entities_called = []

    def fake_llm(messages, cfg=None):
        entities_called.append(1)
        return json.dumps({
            "entities": [{"name": "Alice", "type": "person", "desc": "CEO"}],
            "relations": [],
        })

    monkeypatch.setattr(pipeline.entities, "_llm_request", fake_llm)

    cfg = _extraction_cfg()
    items = [_make_item("a", "text a", ts="2026-01-01T00:00:00Z")]
    item_hash = {"a": "hash-a"}

    pipeline._run_extraction(cfg, fake_driver, items, item_hash, "r1", "fs")

    assert entities_called, "extract() should have been called"
    queries = [q for q, _ in fake_driver.run_calls]
    assert any("Entity" in q for q in queries), \
        f"Entity MERGE not found in: {queries}"


def test_extraction_isolated_per_item_on_failure(monkeypatch):
    """A failing item does not abort the remaining items (per-item
    isolation, matching personal-kb's post_sweep pattern)."""
    fake_driver = StubNeo4j()
    item_a = _make_item("a", "text a")
    item_b = _make_item("b", "text b")

    call_count = {"n": 0}

    def fake_llm(messages, cfg=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First item's LLM call fails
            from digital_twins.ingest.entities import ExtractionError
            raise ExtractionError("LLM call failed: 500")
        return json.dumps({
            "entities": [{"name": "Bob", "type": "person", "desc": ""}],
            "relations": [],
        })

    monkeypatch.setattr(pipeline.entities, "_llm_request", fake_llm)
    cfg = _extraction_cfg()
    items = [item_a, item_b]
    item_hash = {"a": "ha", "b": "hb"}

    # Should NOT raise
    pipeline._run_extraction(cfg, fake_driver, items, item_hash, "r1", "fs")

    assert call_count["n"] == 2, "Both items should have been attempted"
    queries = [q for q, _ in fake_driver.run_calls]
    # Item B's entity should have been written
    assert any("Bob" in json.dumps(str(q)) for q in queries) or \
        any("Bob" in str(p) for _, p in fake_driver.run_calls), \
        "Item B's entity should have been written despite item A failing"


def test_extraction_skipped_when_no_driver(monkeypatch):
    """neo4j=None (Qdrant-only run) → extraction is skipped entirely."""
    llm_calls = []

    def fake_llm(messages, cfg=None):
        llm_calls.append(1)
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(pipeline.entities, "_llm_request", fake_llm)
    cfg = _extraction_cfg()
    items = [_make_item("a", "text a")]
    item_hash = {"a": "hash-a"}

    pipeline._run_extraction(cfg, None, items, item_hash, "r1", "fs")

    assert not llm_calls, "No LLM call when no Neo4j driver"
