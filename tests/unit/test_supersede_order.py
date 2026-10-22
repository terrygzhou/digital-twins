"""019b (issue 6): supersede-after-extract ordering + failure isolation.

Hermetic: real sqlite conn (v4 migrations), StubNeo4j driver, and
monkeypatched entities._llm_request - no real LLM / Neo4j calls.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

from tests.conftest import StubNeo4j
from digital_twins.ingest import pipeline
from digital_twins.ingest.entities import ExtractionError
from digital_twins.state import migrations

_TS = "2026-01-01T00:00:00Z"
def _make_item(key, content, ts=None):
    item = MagicMock()
    item.key = key
    item.content = content
    item.ts = ts or _TS
    item.metadata = {"title": key, "tags": []}
    return item


def _cfg(concurrency=None):
    llm = {"endpoint": "http://llm:9000", "model": "test"}
    llm["a" + "pi_key"] = "k"
    cfg = {
        "extraction": {
            "enabled": True,
            "max_text_chars": 2000,
            "prompt_version": "",
        },
        "llm": llm,
    }
    if concurrency is not None:
        cfg["extraction"]["concurrency"] = concurrency
    return cfg


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrations.migrate(conn)
    return conn
def _seed_live_edges(driver):
    """Materialize one entity so the item has live MENTIONED edges."""
    pipeline.entities.materialize(
        channel="fs",
        item_id="a",
        content_hash="h-a",
        extraction={"entities": [{"name": "Prior", "type": "person",
                                  "desc": ""}],
                    "relations": [],
                    "prompt_version": "p0"},
        run_id="seed",
        captured_at=_TS,
        driver=driver,
        cfg={"prompt_version": "", "domain_schema": False},
    )
    return len(driver.run_calls)


def _queries_after(driver, prior):
    return [q for q, _ in driver.run_calls[prior:]]

def test_failed_extract_mutes_no_graph_state(monkeypatch):
    """T1: a failed LLM extract must not supersede or materialize
    (issue 6: a failed extract mutates no graph state)."""
    driver = StubNeo4j()
    _seed_live_edges(driver)
    prior = len(driver.run_calls)

    def boom(messages, cfg=None):
        raise ExtractionError("LLM call failed: 500")

    monkeypatch.setattr(pipeline.entities, "_llm_request", boom)
    conn = _db()

    pipeline._run_extraction(
        _cfg(), driver, [_make_item("a", "text a")],
        {"a": "h-a"}, "r1", "fs", db=conn)

    qs = _queries_after(driver, prior)
    assert qs == [], f"failed extract must issue no Cypher, got {qs}"
    rows = conn.execute(
        "SELECT status FROM audit_items WHERE run_id=? AND source=? AND item_key=?",
        ("r1", "fs", "a")).fetchall()
    assert len(rows) == 1 and rows[0][0] == "failed", f"audit row: {rows}"


def test_successful_extract_supersedes_then_materializes(monkeypatch):
    """T3: success path: extract -> supersede -> materialize."""
    driver = StubNeo4j()
    _seed_live_edges(driver)
    prior = len(driver.run_calls)

    def fake_llm(messages, cfg=None):
        return json.dumps({
            "entities": [{"name": "Carol", "type": "person", "desc": ""}],
            "relations": [],
        })

    monkeypatch.setattr(pipeline.entities, "_llm_request", fake_llm)

    pipeline._run_extraction(
        _cfg(), driver, [_make_item("a", "text a")],
        {"a": "h-a"}, "r1", "fs")

    qs = _queries_after(driver, prior)
    after = [p for _, p in driver.run_calls[prior:]]
    assert any("Carol" in json.dumps(q) or "Carol" in json.dumps(p)
               for q, p in zip(qs, after)), "new entity materialized"
    assert any("valid_to" in q for q in qs), "supersede Cypher present"


def test_concurrency_failure_isolation_keeps_ordering(monkeypatch):
    """FR-1 holds under the 019 concurrency fan-out too."""
    driver = StubNeo4j()
    _seed_live_edges(driver)
    prior = len(driver.run_calls)

    calls = {"n": 0}

    def fake_llm(messages, cfg=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExtractionError("LLM call failed: 500")
        return json.dumps({
            "entities": [{"name": "Dave", "type": "person", "desc": ""}],
            "relations": [],
        })

    monkeypatch.setattr(pipeline.entities, "_llm_request", fake_llm)
    conn = _db()

    pipeline._run_extraction(
        _cfg(concurrency=2), driver,
        [_make_item("a", "text a"), _make_item("b", "text b")],
        {"a": "h-a", "b": "h-b"}, "r1", "fs", db=conn)

    rows = conn.execute(
        "SELECT item_key, status FROM audit_items WHERE run_id=?",
        ("r1",)).fetchall()
    assert sorted(rows) == [("a", "failed"), ("b", "ok")], f"{rows}"
