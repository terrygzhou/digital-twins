"""019b (issue 6, FR-3): kb_extraction_heal targeted re-extract.

Hermetic: real sqlite conn (v4 migrations), StubNeo4j driver, fake
Qdrant, and monkeypatched entities._llm_request - no real services.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.conftest import StubNeo4j
from digital_twins.ingest import pipeline
from digital_twins.state import models, migrations

_TS = "2026-01-01T00:00:00Z"


def _cfg():
    llm = {"endpoint": "http://llm:9000", "model": "test"}
    llm["a" + "pi_key"] = "k"
    return {
        "extraction": {
            "enabled": True,
            "max_text_chars": 2000,
            "prompt_version": "",
        },
        "llm": llm,
    }


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrations.migrate(conn)
    return conn


class FakeQdrant:
    def __init__(self, payloads=None):
        self._payloads = payloads or {}

    def retrieve(self, collection, ids, with_payload=False):
        pts = [SimpleNamespace(id=i, payload=self._payloads.get(i, {}))
               for i in ids if i in self._payloads]
        return SimpleNamespace(points=pts)


def _payload(key, content):
    return {
        "full_content": content,
        "text": content,
        "content_hash": "h-" + key,
        "captured_at": _TS,
        "source_title": key,
        "item_key": key,
        "ts": _TS,
    }


def test_heal_reextracts_only_latest_failed(monkeypatch):
    driver = StubNeo4j()
    conn = _db()
    # Latest-per-item state: a=failed (heal target), b=ok (skip),
    # c has an older failed row but a newer ok row (skip).
    models.record_audit_item(conn, "r0", "fs", "a", "failed", "boom")
    models.record_audit_item(conn, "r0", "fs", "b", "ok")
    models.record_audit_item(conn, "r0", "fs", "c", "failed", "boom")
    models.record_audit_item(conn, "r1", "fs", "c", "ok")

    monkeypatch.setattr(pipeline.entities, "_llm_request",
                       lambda m, cfg=None: json.dumps({
                           "entities": [{"name": "Zed", "type": "person",
                                         "desc": ""}],
                           "relations": []}))
    qdrant = FakeQdrant({pipeline.point_id_s4("fs", "a", 0): _payload("a", "content a")})

    out = pipeline.heal_extraction(_cfg(), conn, qdrant, driver, "heal-x")

    assert out == {"healed": 1, "failed": 0, "skipped": []}, out
    rows = conn.execute(
        "SELECT item_key, status FROM audit_items WHERE run_id=?",
        ("heal-x",)).fetchall()
    assert rows == [("a", "ok")], rows
    assert any("Zed" in json.dumps(p) for _, p in driver.run_calls), "materialized"


def test_heal_still_failed_records_new_failed_row(monkeypatch):
    driver = StubNeo4j()
    conn = _db()
    models.record_audit_item(conn, "r0", "fs", "a", "failed", "boom")

    def boom(messages, cfg=None):
        raise pipeline.entities.ExtractionError("LLM call failed: 500")

    monkeypatch.setattr(pipeline.entities, "_llm_request", boom)
    qdrant = FakeQdrant({pipeline.point_id_s4("fs", "a", 0): _payload("a", "c")})

    out = pipeline.heal_extraction(_cfg(), conn, qdrant, driver, "heal-x")

    assert out == {"healed": 0, "failed": 1, "skipped": []}, out
    rows = conn.execute(
        "SELECT item_key, status FROM audit_items WHERE run_id=?",
        ("heal-x",)).fetchall()
    assert rows == [("a", "failed")], rows
    # A re-failed heal mutates no graph state (FR-1 holds in heal too).
    assert driver.run_calls == [], driver.run_calls


def test_heal_skips_locked_source():
    driver = StubNeo4j()
    conn = _db()
    models.record_audit_item(conn, "r0", "fs", "a", "failed", "boom")
    models.acquire_ingest_lock(conn, "fs", "other-run")

    out = pipeline.heal_extraction(_cfg(), conn, FakeQdrant(), driver, "heal-x")

    assert out["skipped"] == ["fs"], out
    assert out["healed"] == 0 and out["failed"] == 0, out


def test_heal_no_content_in_qdrant_counts_failed():
    driver = StubNeo4j()
    conn = _db()
    models.record_audit_item(conn, "r0", "fs", "a", "failed", "boom")

    out = pipeline.heal_extraction(_cfg(), conn, FakeQdrant(), driver, "heal-x")

    assert out == {"healed": 0, "failed": 1, "skipped": []}, out
    row = conn.execute(
        "SELECT error FROM audit_items WHERE run_id=?", ("heal-x",)).fetchone()
    assert row is not None and "no content" in row[0], row


def test_heal_noop_when_gate_off_or_no_driver():
    conn = _db()
    models.record_audit_item(conn, "r0", "fs", "a", "failed", "boom")

    cfg_off = _cfg()
    cfg_off["extraction"]["enabled"] = False
    out = pipeline.heal_extraction(cfg_off, conn, FakeQdrant(), StubNeo4j(), "h")
    assert out == {"healed": 0, "failed": 0, "skipped": []}, out

    out = pipeline.heal_extraction(_cfg(), conn, FakeQdrant(), None, "h")
    assert out == {"healed": 0, "failed": 0, "skipped": []}, out

    # Gate off / no driver / no pending rows -> clean no-op.
    fresh = _db()
    out = pipeline.heal_extraction(_cfg(), fresh, FakeQdrant(), StubNeo4j(), "h")
    assert out == {"healed": 0, "failed": 0, "skipped": []}, out
