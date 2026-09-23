"""Unit tests for digital_twins.ingest.graph_query (S4 graph read path).

Hermetic: uses the StubNeo4j fixture from conftest.py (driver-shaped
object with .run() and .session() support) and recording fakes — no
network, no host values (portability-safe, NFR-13).
"""
from __future__ import annotations

import pytest

from digital_twins.ingest import graph_query
from tests.conftest import StubNeo4j


# --- graph_query.expand_relatives (S4: channel-sibling items) --------------

def test_expand_relatives_queries_correct_cypher():
    """expand_relatives issues the pinned S4 sibling-item Cypher with the
    item_id and limit params."""
    fake = StubNeo4j()
    graph_query.expand_relatives(fake, "a", limit=3)
    queries = [q for q, _ in fake.run_calls]
    assert any(graph_query.EXPAND_RELATIVES in q for q in queries), (
        f"expected {graph_query.EXPAND_RELATIVES!r} in queries: {queries}")
    params_list = [p for q, p in fake.run_calls if "OPTIONAL MATCH" in q]
    assert params_list and params_list[0].get("limit") == 3
    assert params_list[0].get("hit") == "a"


def test_expand_relatives_returns_dicts_from_stub_rows():
    """When the stub has stub_rows set, expand_relatives returns them
    as plain dicts."""
    fake = StubNeo4j()
    fake.stub_rows = [
        {"item_id": "b", "content_hash": "h-b"}]
    rows = graph_query.expand_relatives(fake, "a")
    assert rows == [{"item_id": "b", "content_hash": "h-b"}]


# --- graph_query.provenance -------------------------------------------------

def test_provenance_returns_item_metadata():
    fake = StubNeo4j()
    fake.stub_rows = [
        {"item_id": "a", "channel": "fs", "content_hash": "h-a"}
    ]
    result = graph_query.provenance(fake, "a")
    assert result["channel"] == "fs"
    assert result["content_hash"] == "h-a"


def test_provenance_returns_none_on_empty():
    fake = StubNeo4j()
    fake.stub_rows = []
    assert graph_query.provenance(fake, "a") is None
