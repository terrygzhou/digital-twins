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


# --- feature 019 (ingest reliability) task 1.2 ---


def _materialized_entities(driver):
    """Collect the set of entity names materialized on the driver."""
    names = set()
    for q, params in driver.run_calls:
        if "Entity" in q:
            for k in ("name", "n"):
                v = params.get(k)
                if v:
                    names.add(v)
    return names


def test_extraction_concurrency_matches_serial(monkeypatch):
    """extraction.concurrency=4 materializes the same entity set as =1."""
    def run_with(concurrency):
        fake_driver = StubNeo4j()
        calls = []

        def fake_llm(messages, cfg=None):
            calls.append(1)
            return json.dumps({
                "entities": [
                    {"name": "Alice", "type": "person", "desc": "CEO"},
                    {"name": "Bob", "type": "person", "desc": ""},
                ],
                "relations": [],
            })

        monkeypatch.setattr(pipeline.entities, "_llm_request", fake_llm)
        cfg = _extraction_cfg()
        cfg["extraction"]["concurrency"] = concurrency
        items = [_make_item(f"i{k}", f"text {k}") for k in range(6)]
        item_hash = {f"i{k}": f"h{k}" for k in range(6)}
        pipeline._run_extraction(cfg, fake_driver, items, item_hash,
                                 "r1", "fs")
        return fake_driver, calls

    serial_driver, serial_calls = run_with(1)
    conc_driver, conc_calls = run_with(4)

    assert len(serial_calls) == 6
    assert len(conc_calls) == 6, "every item still gets exactly one LLM call"
    assert (_materialized_entities(serial_driver)
            == _materialized_entities(conc_driver)), \
        "concurrent fan-out must materialize the same entity set"


class _OneShotResp:
    """urlopen stand-in: first call 503, second call 200 with a valid
    chat-completion body; counts calls."""
    calls = 0

    def __init__(self, body: bytes):
        self._body = body

    @classmethod
    def fake(cls, req, timeout=None):
        cls.calls += 1
        import urllib.error
        if cls.calls == 1:
            raise urllib.error.HTTPError(
                req.full_url, 503, "Service Unavailable", {}, None)
        import io
        r = io.BytesIO(cls._body)
        r.status = 200

        class _CM:
            def __enter__(self_inner):
                return r
            def __exit__(self_inner, *a):
                return False
        return _CM()


def test_llm_transport_5xx_then_200_retries_once(monkeypatch):
    """A transient 5xx is retried once; the item then succeeds."""
    import json as _json
    from digital_twins.ingest import entities as ents

    _OneShotResp.calls = 0
    body = _json.dumps({"choices": [{"message": {"content":
        _json.dumps({"entities": [], "relations": []})}}]}).encode()
    _OneShotResp._body = body
    monkeypatch.setattr(ents.urllib.request, "urlopen", _OneShotResp.fake)

    ents._llm_request(
        [{"role": "user", "content": "hi"}],
        cfg={"endpoint": "http://llm:9000/v1", "model": "m",
             "api_key": "k"},
    )
    assert _OneShotResp.calls == 2, \
        f"5xx must be retried exactly once, got {_OneShotResp.calls} calls"


def test_llm_transport_persistent_5xx_fails_after_retry(monkeypatch):
    """Two consecutive 5xx -> ExtractionError after exactly 2 attempts
    (retry-once; no unbounded loop)."""
    import urllib.error
    from digital_twins.ingest import entities as ents
    from digital_twins.ingest.entities import ExtractionError

    class _Always503:
        calls = 0

        @classmethod
        def fake(cls, req, timeout=None):
            cls.calls += 1
            raise urllib.error.HTTPError(
                req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(ents.urllib.request, "urlopen", _Always503.fake)
    with pytest.raises(ExtractionError):
        ents._llm_request(
            [{"role": "user", "content": "hi"}],
            cfg={"endpoint": "http://llm:9000/v1", "model": "m",
                 "api_key": "k"},
        )
    assert _Always503.calls == 2, \
        f"persistent 5xx must stop after one retry, got {_Always503.calls}"


def test_llm_transport_timeout_not_retried(monkeypatch):
    """A timeout (non-HTTPError) is NOT retried - single attempt."""
    import socket
    from digital_twins.ingest import entities as ents
    from digital_twins.ingest.entities import ExtractionError

    class _TimeoutOnce:
        calls = 0

        @classmethod
        def fake(cls, req, timeout=None):
            cls.calls += 1
            raise socket.timeout("read timed out")

    monkeypatch.setattr(ents.urllib.request, "urlopen", _TimeoutOnce.fake)
    with pytest.raises(ExtractionError):
        ents._llm_request(
            [{"role": "user", "content": "hi"}],
            cfg={"endpoint": "http://llm:9000/v1", "model": "m",
                 "api_key": "k"},
        )
    assert _TimeoutOnce.calls == 1, \
        f"timeout must NOT be retried, got {_TimeoutOnce.calls} calls"
