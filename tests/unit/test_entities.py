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


# ── REL edge materialization (018-rel-edge-materialize) ──────────────────


def test_materialize_writes_rel_edges():
    """018: materialize() with 2 relations issues REL MERGE Cypher."""
    fake = StubNeo4j()
    extraction = {
        "entities": [
            {"name": "Alice", "type": "person", "desc": ""},
            {"name": "ACME", "type": "organization", "desc": ""},
            {"name": "Bob", "type": "person", "desc": ""},
            {"name": "Paris", "type": "place", "desc": ""},
        ],
        "relations": [
            {"subject": "Alice", "predicate": "works_for", "object": "ACME"},
            {"subject": "Bob", "predicate": "located_in", "object": "Paris"},
        ],
        "prompt_version": PROMPT_VERSION,
    }
    out = materialize(
        channel="fs", item_id="item-018", content_hash="h",
        extraction=extraction, run_id="r1",
        captured_at="2026-01-01T00:00:00Z", driver=fake, cfg={},
    )
    # REL MERGE queries were issued (one per relation)
    rel_queries = [
        q for q, _ in fake.run_calls
        if "REL" in q and "MERGE" in q
    ]
    assert len(rel_queries) == 2, (
        f"Expected 2 REL MERGE queries, got {len(rel_queries)}: "
        f"{rel_queries}"
    )
    # Return dict carries the relations key
    assert "relations" in out


def test_materialize_writes_domain_types_and_predicates():
    """020: materialize() passes domain entity types and REL predicates
    through to the graph unchanged — `:Entity {type:'competitor'}` and
    `(si)-[REL {predicate:'competes_with'}]` for a domain extraction."""
    import digital_twins.ingest.entities as ent

    fake = StubNeo4j()
    extraction = {
        "entities": [
            {"name": "Rotork", "type": "competitor", "desc": ""},
            {"name": "API 598", "type": "standard", "desc": ""},
        ],
        "relations": [
            {"subject": "Rotork", "predicate": "competes_with",
             "object": "BetaCorp"},
        ],
        "prompt_version": ent.DOMAIN_PROMPT_VERSION,
    }
    out = materialize(
        channel="fs", item_id="item-020", content_hash="h",
        extraction=extraction, run_id="r1",
        captured_at="2026-01-01T00:00:00Z", driver=fake, cfg={},
    )
    # Entity MERGE queries carry the domain type values
    entity_queries = [
        q for q, params in fake.run_calls
        if "Entity" in q and "MERGE" in q
    ]
    entity_params = [p for q, p in fake.run_calls if "Entity" in q]
    assert any("competitor" in str(p) for p in entity_params), (
        f"Expected a competitor entity param, got: {entity_params}")
    assert any("standard" in str(p) for p in entity_params), (
        f"Expected a standard entity param, got: {entity_params}")
    # REL MERGE query carries the domain predicate
    rel_queries = [
        q for q, params in fake.run_calls
        if "REL" in q and "MERGE" in q
    ]
    rel_params = [p for q, p in fake.run_calls if "REL" in q]
    assert any("competes_with" in str(p) for p in rel_params), (
        f"Expected a competes_with REL param, got: {rel_params}")
    assert "relations" in out


def test_materialize_rel_idempotent_rerun():
    """018: re-materialising the same extraction is idempotent (NFR-1).
    StubNeo4j always reports 0 counters, so both runs must show 0."""
    fake = StubNeo4j()
    extraction = {
        "entities": [
            {"name": "Alice", "type": "person", "desc": ""},
            {"name": "ACME", "type": "organization", "desc": ""},
        ],
        "relations": [
            {"subject": "Alice", "predicate": "works_for", "object": "ACME"},
        ],
        "prompt_version": PROMPT_VERSION,
    }
    out1 = materialize(
        channel="fs", item_id="item-018", content_hash="h",
        extraction=extraction, run_id="r1",
        captured_at="t1", driver=fake, cfg={},
    )
    out2 = materialize(
        channel="fs", item_id="item-018", content_hash="h",
        extraction=extraction, run_id="r2",
        captured_at="t2", driver=fake, cfg={},
    )
    # Stub reports 0 for both — the contract is that the key exists
    # and the value is a non-negative int (0 on a no-op stub).
    assert out1["relations"] == 0
    assert out2["relations"] == 0


def test_extract_filters_relation_with_non_entity_endpoint():
    """018: extract() drops relations whose subject or object is not
    in the extracted entity set (guard holds end-to-end)."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [
                {"name": "Alice", "type": "person", "desc": ""},
            ],
            "relations": [
                {"subject": "Alice", "predicate": "works_for",
                 "object": "GhostCorp"},  # not in entity set
            ],
        })

    monkeypatcher = pytest.MonkeyPatch()
    monkeypatcher.setattr(ent, "_llm_request", fake_llm)
    try:
        result = extract("text", cfg={})
    finally:
        monkeypatcher.undo()

    assert result["relations"] == []


def test_materialize_returns_relations_key():
    """018: materialize() return dict always carries the 'relations'
    key, even when there are no relations in the extraction."""
    fake = StubNeo4j()
    extraction = {
        "entities": [
            {"name": "Alice", "type": "person", "desc": ""},
            {"name": "Paris", "type": "place", "desc": ""},
        ],
        "relations": [],
        "prompt_version": PROMPT_VERSION,
    }
    out = materialize(
        channel="fs", item_id="item-018", content_hash="h",
        extraction=extraction, run_id="r1",
        captured_at="2026-01-01T00:00:00Z", driver=fake, cfg={},
    )
    assert "relations" in out
    assert out["relations"] == 0


# ── Domain extraction schema (020-domain-extraction-schema, Tier B) ──────────


def test_domain_entity_types_and_predicates_exported():
    """020: the domain profile constants exist, are exported, and match
    the Tier B spec exactly."""
    import digital_twins.ingest.entities as ent

    assert ent.DOMAIN_ENTITY_TYPES == (
        "product", "customer", "competitor", "standard", "cert",
    )
    assert ent.DOMAIN_PREDICATES == (
        "competes_with", "supplied_to", "complies_with",
        "substitutes_for", "manufactured_by", "tested_to",
    )
    assert "DOMAIN_ENTITY_TYPES" in ent.__all__
    assert "DOMAIN_PREDICATES" in ent.__all__


def test_domain_prompt_version_distinct():
    """020: DOMAIN_PROMPT_VERSION exists and differs from the Tier A
    PROMPT_VERSION (bump = re-extract, so a distinct stamp is required)."""
    import digital_twins.ingest.entities as ent

    assert ent.DOMAIN_PROMPT_VERSION != ""
    assert ent.DOMAIN_PROMPT_VERSION != ent.PROMPT_VERSION


def test_extract_domain_mode_uses_domain_prompt(monkeypatch):
    """020(a): with cfg={"domain_schema": True} the system message sent to
    the LLM is the domain prompt — it mentions the domain types and the
    predicate allowlist, not the generic types."""
    import digital_twins.ingest.entities as ent

    captured = {}

    def fake_llm(messages, cfg=None):
        captured["system"] = [
            m for m in messages if m["role"] == "system"
        ][0]["content"]
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    extract("text", cfg={"domain_schema": True})
    sys_msg = captured["system"]
    assert "competes_with" in sys_msg
    assert "product" in sys_msg
    # generic-only type vocabulary must not appear in the domain prompt
    import re
    assert not re.search(r"person|organization", sys_msg)


def test_extract_domain_mode_entity_type_constraint(monkeypatch):
    """020(b): in domain mode, an entity whose type is not in
    DOMAIN_ENTITY_TYPES (e.g. "material") is discarded while a "product"
    entity is kept."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [
                {"name": "WidgetPro", "type": "product", "desc": ""},
                {"name": "AlloyX", "type": "material", "desc": ""},
            ],
            "relations": [],
        })

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={"domain_schema": True})
    names = [e["name"] for e in result["entities"]]
    assert "WidgetPro" in names
    assert "AlloyX" not in names  # "material" not in DOMAIN_ENTITY_TYPES


def test_extract_domain_mode_predicate_allowlist(monkeypatch):
    """020(c): in domain mode, a free-form predicate ("related_to") is
    dropped while an allowlisted predicate ("competes_with") between two
    kept domain entities is retained."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [
                {"name": "AlphaCorp", "type": "competitor", "desc": ""},
                {"name": "BetaCorp", "type": "competitor", "desc": ""},
            ],
            "relations": [
                {"subject": "AlphaCorp", "predicate": "related_to",
                 "object": "BetaCorp"},
                {"subject": "AlphaCorp", "predicate": "competes_with",
                 "object": "BetaCorp"},
            ],
        })

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={"domain_schema": True})
    preds = [r["predicate"] for r in result["relations"]]
    assert "competes_with" in preds
    assert "related_to" not in preds


def test_extract_domain_mode_prompt_version_stamp(monkeypatch):
    """020(d): the extraction's prompt_version is the domain stamp, not the
    generic one."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={"domain_schema": True})
    assert result["prompt_version"] == ent.DOMAIN_PROMPT_VERSION
    assert result["prompt_version"] != ent.PROMPT_VERSION


def test_extract_pinned_prompt_version_wins_in_domain_mode(monkeypatch):
    """020 spec: a pinned extraction.prompt_version is stamped in domain
    mode too — the pin overrides DOMAIN_PROMPT_VERSION, so a re-extraction
    with the same pin supersedes both profiles' edges of the item."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={"domain_schema": True,
                                  "prompt_version": "2026-08-23.1"})
    assert result["prompt_version"] == "2026-08-23.1"
    assert result["prompt_version"] != ent.DOMAIN_PROMPT_VERSION
    # Unpinned domain mode still stamps the domain default.
    result = extract("text", cfg={"domain_schema": True})
    assert result["prompt_version"] == ent.DOMAIN_PROMPT_VERSION


def test_extract_generic_mode_keeps_freeform_predicate(monkeypatch):
    """Tier A unchanged: in generic mode (no domain_schema) the
    free-form predicate allowlist does NOT apply — "related_to" survives."""
    import digital_twins.ingest.entities as ent

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [
                {"name": "Alice", "type": "person", "desc": ""},
                {"name": "Paris", "type": "place", "desc": ""},
            ],
            "relations": [
                {"subject": "Alice", "predicate": "related_to",
                 "object": "Paris"},
            ],
        })

    monkeypatch.setattr(ent, "_llm_request", fake_llm)
    result = extract("text", cfg={})
    preds = [r["predicate"] for r in result["relations"]]
    assert "related_to" in preds
    assert result["prompt_version"] == ent.PROMPT_VERSION
