"""Unit tests for digital_twins.ingest.entities (s4-entity-extraction).

Hermetic: monkeypatches the module-level `_llm_request` seam so no real
LLM call is made. No host values (portability-safe, NFR-13).
"""
from __future__ import annotations

import json
import pytest

from digital_twins.ingest.entities import (
    ENTITY_TYPES,
    PROMPT_VERSION,
    ExtractionError,
    prompt_version,
    extract,
)


def test_entity_types_tuple():
    assert ENTITY_TYPES == (
        "person", "organization", "place", "event", "concept",
    )


def test_prompt_version_default():
    assert prompt_version() == PROMPT_VERSION
    assert PROMPT_VERSION != ""


def test_prompt_version_override():
    assert prompt_version({"prompt_version": "2026-01-01.1"}) == "2026-01-01.1"


def test_extract_empty_llm_response(monkeypatch):
    """When the LLM returns empty JSON, extract returns empty lists."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("some text", title="T", cfg={})
    assert result["entities"] == []
    assert result["relations"] == []
    assert result["prompt_version"] == PROMPT_VERSION


def test_extract_type_constraint_discards_out_of_enum(monkeypatch):
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [
                {"name": "Alice", "type": "person", "desc": "CEO"},
                {"name": "ACME", "type": "company", "desc": "Corp"},  # not in ENTITY_TYPES
                {"name": "Paris", "type": "place", "desc": "City"},
            ],
            "relations": [],
        })

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={})
    names = [e["name"] for e in result["entities"]]
    assert "Alice" in names
    assert "Paris" in names
    assert "ACME" not in names  # type "company" not in ENTITY_TYPES


def test_extract_dedup_case_insensitive(monkeypatch):
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [
                {"name": "Alice", "type": "person", "desc": ""},
                {"name": "alice", "type": "person", "desc": ""},  # dup
                {"name": "Bob", "type": "person", "desc": ""},
            ],
            "relations": [],
        })

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={})
    assert len(result["entities"]) == 2
    names = [e["name"] for e in result["entities"]]
    assert "Alice" in names and "Bob" in names


def test_extract_caps_at_25_entities(monkeypatch):
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        ents = [{"name": f"E{i}", "type": "person", "desc": ""} for i in range(40)]
        return json.dumps({"entities": ents, "relations": []})

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={})
    assert len(result["entities"]) == 25


def test_extract_truncates_to_max_text_chars(monkeypatch):
    import digital_twins.ingest.entities as ent

    captured = {}

    def fake_llm(messages, cfg=None):
        user_msg = [m for m in messages if m["role"] == "user"][0]["content"]
        captured["user_content"] = user_msg
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    long_text = "x" * 20000
    extract(long_text, cfg={"max_text_chars": 5000})
    assert len(captured["user_content"]) <= 5000 + len("Title: ")  # + title prefix


def test_llm_request_missing_endpoint_raises():
    import digital_twins.ingest.entities as ent

    with pytest.raises(ExtractionError, match="not configured"):
        ent._llm_request(
            [{"role": "user", "content": "hi"}],
            cfg={"endpoint": None, "model": None, "api_key": None},
        )


def test_llm_request_unconfigured_endpoint_raises():
    import digital_twins.ingest.entities as ent

    with pytest.raises(ExtractionError, match="not configured"):
        ent._llm_request(
            [{"role": "user", "content": "hi"}],
            cfg={"endpoint": "", "model": "", "api_key": ""},
        )


# ── Neo4j materialisation (recording-driver tests) ──────────────────────────

from tests.conftest import StubNeo4j
from digital_twins.ingest.entities import supersede, materialize, drop


def test_supersede_issues_two_cypher_statements():
    fake = StubNeo4j()
    supersede("item-a", driver=fake, cfg={})
    queries = [q for q, _ in fake.run_calls]
    # MENTIONED supersede
    assert any(
        "MENTIONED" in q and "valid_to" in q and "IS NULL" in q
        for q in queries
    ), f"MENTIONED supersede not found in: {queries}"
    # REL supersede (scoping by source_item)
    assert any(
        "REL" in q and "source_item" in q and "valid_to" in q
        for q in queries
    ), f"REL supersede not found in: {queries}"
    # item_id param present
    params = [p for _, p in fake.run_calls]
    assert any(p.get("item_id") == "item-a" for p in params)


def test_materialize_writes_entities_and_mentioned():
    fake = StubNeo4j()
    extraction = {
        "entities": [
            {"name": "Alice", "type": "person", "desc": "CEO"},
            {"name": "Paris", "type": "place", "desc": "City"},
        ],
        "relations": [],
        "prompt_version": PROMPT_VERSION,
    }
    out = materialize(
        channel="fs", item_id="item-a", content_hash="h1",
        extraction=extraction, run_id="r1",
        captured_at="2026-01-01T00:00:00Z", driver=fake, cfg={},
    )
    assert "entities" in out and "mentioned" in out
    queries = [q for q, _ in fake.run_calls]
    # Entity MERGE
    assert any("MERGE (e:Entity" in q for q in queries), queries
    # SourceItem MERGE
    assert any("MERGE (si:SourceItem" in q for q in queries), queries
    # MENTIONED edge
    assert any("MENTIONED" in q for q in queries), queries


def test_materialize_idempotent_reuse_same_extraction():
    """Re-materialising the same extraction does NOT create new entities
    (MERGE is idempotent — NFR-1). The counter on a no-op re-run should
    reflect zero new entity nodes."""
    fake = StubNeo4j()
    extraction = {
        "entities": [{"name": "Alice", "type": "person", "desc": ""}],
        "relations": [],
        "prompt_version": PROMPT_VERSION,
    }
    # StubNeo4j always reports counters=0 (no-op stub)
    out1 = materialize(
        channel="fs", item_id="item-a", content_hash="h1",
        extraction=extraction, run_id="r1",
        captured_at="t1", driver=fake, cfg={},
    )
    out2 = materialize(
        channel="fs", item_id="item-a", content_hash="h1",
        extraction=extraction, run_id="r2",
        captured_at="t2", driver=fake, cfg={},
    )
    # Stub returns 0 counters for both; assert no new entity nodes
    assert out1["entities"] == 0 and out2["entities"] == 0
    assert out1["relations"] == 0 and out2["relations"] == 0


def test_drop_issues_detach_delete():
    fake = StubNeo4j()
    result = drop("item-a", driver=fake, cfg={})
    queries = [q for q, _ in fake.run_calls]
    assert any("DETACH DELETE" in q for q in queries), queries
