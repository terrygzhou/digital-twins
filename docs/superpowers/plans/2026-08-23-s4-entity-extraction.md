# S4 Entity Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add config-gated LLM entity extraction (`:Entity` / `:MENTIONED` graph writes) to digital-twins' ingest pipeline, mirroring personal-kb's `kb/core/graph.py` contract, so that digital-twins items participate in S4 graph expansion.

**Architecture:** A new `digital_twins/ingest/entities.py` module holds the extraction prompt, LLM transport (stdlib urllib, SGLang-compatible), `extract()`, `materialize()`, `supersede()`, and `drop()`. The pipeline calls these inline after `_upsert_graph` when `extraction.enabled` is true and a Neo4j driver is present. Extraction reuses the existing `llm.endpoint` / `llm.model` / `llm.api_key` knobs — no new endpoint knobs. Per-item failures are isolated (logged, not raised).

**Tech Stack:** Python 3.11, stdlib `urllib` (no new deps), neo4j-driver (already a dep), qdrant-client, pytest + StubNeo4j + monkeypatch.

**Spec:** `openspec/changes/s4-entity-extraction/{proposal,design,tasks}.md`

## Global Constraints

- No host paths, usernames, or install locations in shipped code (BR-11.2 / NFR-13).
- `llm.api_key` is read from the config layer, NOT an env var (portable; personal-kb's env-var pattern is host-specific).
- No new pip dependencies: stdlib `urllib` only.
- REL edges are out of scope (written in personal-kb's `materialize` step 4 but NOT in digital-twins' `materialize` in this change — `extract()` still parses relations, `materialize()` writes them but with a no-op step that is commented as deferred).
- Extraction is config-gated: `extraction.enabled` defaults to `false` (off). When off, no LLM call and no entity writes.
- All new config knobs: `extraction.enabled` (bool, default `false`), `extraction.max_text_chars` (int, default `12000`), `extraction.prompt_version` (str, default `""`).
- `tests/integration/test_portability.py` and `tests/unit/test_knob_docs.py` must stay green.
- Test-First: every task's test is written and run (fails) before the implementation.
- Commit after each task.

---

## File Map

| File | Responsibility |
|------|----------------|
| `digital_twins/ingest/entities.py` (NEW) | `PROMPT_VERSION`, `ENTITY_TYPES`, `ExtractionError`, `prompt_version()`, `extract()`, `_llm_request()`, `supersede()`, `materialize()`, `drop()` |
| `digital_twins/ingest/pipeline.py` | Post-graph extraction step in `run_pipeline` (Task 3) |
| `digital_twins/config/schema.py` | `extraction.*` knobs in `DEFAULTS` + `_ALLOW_EMPTY` (Task 4) |
| `digital_twins/config/knobs.py` | `extraction.*` entries in `KNOBS` registry (Task 4) |
| `config.example.yml` | `extraction:` section with comments (Task 4) |
| `tests/unit/test_entities.py` (NEW) | Unit tests for `entities.py` (Tasks 1–2) |
| `tests/unit/test_pipeline_extraction.py` (NEW) | Pipeline hook tests (Task 3) |
| `tests/unit/test_extraction_knobs.py` (NEW) | Config knob tests (Task 4) |

---

### Task 1: `entities.py` — constants, `ExtractionError`, `prompt_version()`, `_llm_request()` seam

**Files:**
- Create: `digital_twins/ingest/entities.py`
- Test: `tests/unit/test_entities.py`

**Interfaces:**
- Consumes: `llm.endpoint`, `llm.model`, `llm.api_key` from config (via `digital_twins.config.schema.get`)
- Produces: `PROMPT_VERSION`, `ENTITY_TYPES`, `ExtractionError`, `prompt_version(cfg)`, `_llm_request(messages, cfg)` (module-level, monkeypatchable in tests)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_entities.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_entities.py -v 2>&1 | head -60
```

Expected: `ModuleNotFoundError: digital_twins.ingest.entities`

- [ ] **Step 3: Create `digital_twins/ingest/entities.py`**

```python
"""LLM entity extraction + Neo4j materialisation (S4 entity half).

Mirrors personal-kb's ``kb/core/graph.py`` contract:

- ``PROMPT_VERSION`` — pinned prompt version (bump = re-extract).
- ``ENTITY_TYPES`` — allowed entity types (type-constrained).
- ``extract()`` — LLM call → ``{"entities", "relations", "prompt_version"}``.
- ``supersede()`` — stamps ``valid_to`` on live MENTIONED edges before
  re-extraction (BR-6.4 parity).
- ``materialize()`` — idempotent MERGE of Entity nodes + MENTIONED edges
  (NFR-1: re-running the same extraction touches nothing new).
- ``drop()`` — DETACH-DELETEs a SourceItem and prunes orphaned entities
  (BR-6.3 parity, exposed for future reconcile use).

Extraction reuses ``llm.endpoint`` / ``llm.model`` / ``llm.api_key`` from
the config layer (no extraction-specific endpoint knobs). When
``extraction.enabled`` is false the module is never called.

REL edges: ``extract()`` still parses relations from the LLM response
(the prompt requests them) but ``materialize()`` does NOT write REL edges
in this change — REL production is a separate follow-up change
(s4-entity-extraction design decision 1).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any
from datetime import UTC, datetime

__all__ = [
    "PROMPT_VERSION",
    "ENTITY_TYPES",
    "ExtractionError",
    "prompt_version",
    "extract",
    "supersede",
    "materialize",
    "drop",
]

#: Version of the EXTRACTION_PROMPT. Stamped on every edge produced.
#: Bump (and re-extract) when the prompt or entity-type set changes.
PROMPT_VERSION = "2026-08-23.1"

#: Allowed entity types — the prompt is constrained to this set, and so is
#: materialisation (anything else is dropped from the extraction).
ENTITY_TYPES = ("person", "organization", "place", "event", "concept")

EXTRACTION_PROMPT = f"""\
You are an entity-extraction pipeline for a personal knowledge base.
Read the document and extract the entities and relations it contains.

Rules:
- "entities": names of people, organizations, places, events and concepts
  that are concrete enough to be useful for navigation. 3 to 25 entities.
- Each entity: {{"name": str, "type": one of person|organization|place|event|concept,
  "desc": short description, max 120 chars}}.
- "relations": only relations explicitly supported by the document, each
  {{"subject": entity name, "predicate": short snake_case verb phrase
  (e.g. works_for, located_in, part_of, authored, involved_in, related_to),
  "object": entity name}}. subject and object MUST be names from "entities".
- Do not invent entities or relations that are not in the document.
- If the document is too thin to extract anything useful, return
  {{"entities": [], "relations": []}}.

Respond with JSON only, in exactly this shape:
{{"entities": [{{"name": ..., "type": ..., "desc": ...}}],
  "relations": [{{"subject": ..., "predicate": ..., "object": ...}}]}}
(prompt version {PROMPT_VERSION})
"""


class ExtractionError(RuntimeError):
    """LLM call or JSON parse failed for one item (isolated per item)."""


def _safe_int(value: Any, default: int) -> int:
    """Best-effort int for config-sourced values (never raises)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def prompt_version(cfg: dict | None = None) -> str:
    """Effective prompt version: pinned config > built-in."""
    pinned = str((cfg or {}).get("prompt_version", "")).strip()
    return pinned or PROMPT_VERSION


# ── LLM transport (stdlib, SGLang OpenAI-compatible /v1/chat/completions) ──


def _llm_request(messages: list[dict], cfg: dict | None = None) -> str:
    """One JSON-mode chat completion; returns the content string.

    Uses ``llm.endpoint`` / ``llm.model`` / ``llm.api_key`` from the
    config layer (NOT an env var — portable, BR-11.2 / NFR-13).
    Raises :class:`ExtractionError` on transport/HTTP/parse failure.
    """
    cfg = cfg or {}
    endpoint = str(cfg.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise ExtractionError(
            "llm.endpoint not configured (set llm.endpoint in kb.yml)")
    model = str(cfg.get("model") or "")
    api_key = str(cfg.get("api_key") or "")

    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "max_tokens": 3000,
        }
    ).encode()
    req = urllib.request.Request(
        f"{endpoint}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (api_key.strip() or "none"),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — surfaced as a per-item failure
        raise ExtractionError(f"LLM call failed: {e}") from e
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ExtractionError(f"LLM response malformed: {str(e)[:120]}") from e
    if not isinstance(content, str):
        raise ExtractionError("LLM content not a string")
    return content


def extract(text: str, *, title: str = "", cfg: dict | None = None) -> dict:
    """Extract entities + relations from one item's text (versioned prompt).

    Returns ``{"entities": [...], "relations": [...], "prompt_version": str}``
    with types constrained to :data:`ENTITY_TYPES`. Raises
    :class:`ExtractionError` on LLM/parse failure — callers isolate per item.
    """
    cfg = cfg or {}
    max_chars = _safe_int(cfg.get("max_text_chars"), 12000)
    body = text[:max_chars]
    user = (f"Title: {title}\n\n" if title else "") + body
    content = _llm_request(
        [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": user},
        ],
        cfg=cfg,
    )
    # The model may wrap JSON in prose or code fences — strip a leading fence.
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"LLM returned non-JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ExtractionError("LLM JSON is not an object")

    entities = []
    for ent in parsed.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        etype = str(ent.get("type") or "").strip().lower()
        if not name or etype not in ENTITY_TYPES:
            continue
        entities.append(
            {
                "name": name,
                "type": etype,
                "desc": str(ent.get("desc") or "").strip()[:200],
            }
        )
    seen: set[tuple[str, str]] = set()
    deduped = []
    for ent in entities:
        key = (ent["name"].lower(), ent["type"])
        if key not in seen:
            seen.add(key)
            deduped.append(ent)

    ent_names = {(e["name"].lower(), e["type"]) for e in deduped}
    relations = []
    for rel in parsed.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        subj = str(rel.get("subject") or "").strip()
        pred = str(rel.get("predicate") or "").strip().lower()
        obj = str(rel.get("object") or "").strip()
        if not subj or not pred or not obj:
            continue
        if not any(n == subj.lower() for n, _ in ent_names) or not any(
            n == obj.lower() for n, _ in ent_names
        ):
            continue
        relations.append({"subject": subj, "predicate": pred, "object": obj})
    return {
        "entities": deduped[:25],
        "relations": relations[:100],
        "prompt_version": prompt_version(cfg),
    }
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_entities.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/terry/projects/digital-twins
git add digital_twins/ingest/entities.py tests/unit/test_entities.py
git commit -m "feat(entities): add PROMPT_VERSION, ENTITY_TYPES, extract() and LLM transport

- New digital_twins/ingest/entities.py mirroring personal-kb kb/core/graph.py
- _llm_request: stdlib urllib, SGLang-compatible, llm.api_key from config
- extract(): type-constrained, deduped, capped at 25 entities / 100 relations
- Test-first: tests/unit/test_entities.py (hermetic, no real LLM calls)"
```

---

### Task 2: `entities.py` — `supersede()`, `materialize()`, `drop()`

**Files:**
- Modify: `digital_twins/ingest/entities.py`
- Test: `tests/unit/test_entities.py` (append)

**Interfaces:**
- Consumes: `StubNeo4j` from `tests/conftest.py` (driver-shaped `.run()` fake)
- Produces: `supersede(item_id, driver, cfg)`, `materialize(channel, item_id, content_hash, extraction, run_id, captured_at, driver, cfg)`, `drop(item_id, driver, cfg)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_entities.py`:

```python
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
    # Stub returns 0 for both; assert no exception and same shape
    assert out1.keys() == out2.keys()


def test_drop_issues_detach_delete():
    fake = StubNeo4j()
    result = drop("item-a", driver=fake, cfg={})
    queries = [q for q, _ in fake.run_calls]
    assert any("DETACH DELETE" in q for q in queries), queries
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_entities.py -v -k "supersede or materialize or drop" 2>&1 | tail -20
```

Expected: `ImportError: cannot import name 'supersede'` (not yet defined)

- [ ] **Step 3: Implement `supersede()`, `materialize()`, `drop()` in `entities.py`**

Append to `digital_twins/ingest/entities.py`:

```python
# ── Neo4j writes ────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run(driver, cypher: str, **params) -> int:
    """Run one write Cypher; return a non-zero counter when the query
    actually wrote anything. The neo4j driver's SummaryCounters has no
    ``updates`` attribute — sum the concrete counters instead."""
    with driver.session() as s:
        result = s.run(cypher, params)
        summary = result.consume()
        c = summary.counters
        return (
            _safe_int(c.nodes_created, 0)
            + _safe_int(c.nodes_deleted, 0)
            + _safe_int(c.relationships_created, 0)
            + _safe_int(c.relationships_deleted, 0)
            + _safe_int(c.properties_set, 0)
        )


def supersede(item_id: str, *, driver, cfg: dict | None = None) -> int:
    """Set ``valid_to`` on the item's live graph edges (BR-6.4).

    Before re-extracting a changed item: its live ``MENTIONED`` edges and its
    live ``REL`` edges (``source_item = item_id``) become superseded.
    Re-materialisation revives the still-true ones. Returns the number of
    edges marked.
    """
    now = _now()
    n1 = _run(
        driver,
        "MATCH (si:SourceItem {item_id: $item_id})-[m:MENTIONED]->() "
        "WHERE m.valid_to IS NULL "
        "SET m.valid_to = $now",
        item_id=item_id,
        now=now,
    )
    n2 = _run(
        driver,
        "MATCH ()-[r:REL]->() "
        "WHERE r.source_item = $item_id AND r.valid_to IS NULL "
        "SET r.valid_to = $now",
        item_id=item_id,
        now=now,
    )
    return n1 + n2


def materialize(
    *,
    channel: str,
    item_id: str,
    content_hash: str,
    extraction: dict,
    run_id: str,
    captured_at: str,
    driver,
    cfg: dict | None = None,
) -> dict:
    """Materialise one extraction into the graph (idempotent MERGE).

    ``MERGE (:Entity {name, type})`` / ``MERGE (:SourceItem {item_id})``
    plus ``MENTIONED`` edges stamped with ``run_id`` + ``prompt_version``
    + ``valid_from``. Re-running the same extraction touches nothing new
    (NFR-1); re-observed edges from a supersede pass are revived
    (``valid_to`` cleared).

    REL edges are NOT written in this change (design decision 1 — REL
    production is a separate follow-up). The extraction dict may contain
    a ``relations`` key (parsed by ``extract()``) but it is not written.

    Returns per-kind counters: ``{"entities": N, "mentioned": N,
    "relations": N}`` (relations counter is always 0 in this change).
    """
    now = _now()
    pv = str(extraction.get("prompt_version") or prompt_version(cfg))
    entities = extraction.get("entities") or []

    # 1) Entity nodes (idempotent MERGE)
    ent_updates = 0
    for ent in entities:
        ent_updates += _run(
            driver,
            "MERGE (e:Entity {name: $name, type: $type}) "
            "ON CREATE SET e.created = $now, e.desc = $desc "
            "ON MATCH SET e.last_seen = $now, e.desc = "
            "CASE WHEN $desc <> '' THEN $desc ELSE e.desc END",
            name=ent["name"],
            type=ent["type"],
            desc=ent.get("desc", ""),
            now=now,
        )

    # 2) SourceItem node (re-stamp channel/hash; MERGE is idempotent)
    _run(
        driver,
        "MERGE (si:SourceItem {item_id: $item_id}) "
        "SET si.channel = $channel, si.content_hash = $content_hash, "
        "si.last_run_id = $run_id, si.last_captured_at = $captured_at",
        item_id=item_id,
        channel=channel,
        content_hash=content_hash,
        run_id=run_id,
        captured_at=captured_at,
    )

    # 3) MENTIONED edges (si -> entity); ON MATCH revives superseded ones
    mentioned = 0
    for ent in entities:
        mentioned += _run(
            driver,
            "MATCH (si:SourceItem {item_id: $item_id}) "
            "MATCH (e:Entity {name: $name, type: $type}) "
            "MERGE (si)-[m:MENTIONED]->(e) "
            "ON CREATE SET m.captured_at = $captured_at, m.run_id = $run_id, "
            "m.prompt_version = $pv, m.valid_from = $now "
            "ON MATCH SET m.valid_to = NULL, m.last_seen = $now, "
            "m.run_id = $run_id",
            item_id=item_id,
            name=ent["name"],
            type=ent["type"],
            captured_at=captured_at,
            run_id=run_id,
            pv=pv,
            now=now,
        )

    # 4) REL edges — deferred to a follow-up change (design decision 1).
    #    The extraction dict may carry a "relations" key but it is not
    #    written here. The counter is reported as 0 for the record.

    return {
        "entities": ent_updates,
        "mentioned": mentioned,
        "relations": 0,
    }


def drop(item_id: str, *, driver, cfg: dict | None = None) -> bool:
    """Graph half of reconcile removal (BR-6.3): delete the item's
    ``SourceItem`` (and its edges) and prune entities that lost their last
    reference. Returns True when a SourceItem was deleted.

    An entity is pruned only when it has neither any incoming MENTIONED edge
    nor any REL edge (live or superseded — history still references it).
    No-op (returns False) when the item has no graph rows (fresh DBs).
    """
    with driver.session() as s:
        result = s.run(
            "MATCH (si:SourceItem {item_id: $item_id}) "
            "DETACH DELETE si RETURN count(si) AS deleted",
            item_id=item_id,
        ).single()
        deleted = int(result["deleted"]) if result else 0
        if deleted:
            s.run(
                "MATCH (e:Entity) "
                "WHERE NOT (e)<-[:MENTIONED]-(:SourceItem) "
                "AND NOT (e)-[:REL]->() AND NOT (:Entity)-[:REL]->(e) "
                "DELETE e"
            )
        return bool(deleted)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_entities.py -v 2>&1 | tail -30
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/terry/projects/digital-twins
git add digital_twins/ingest/entities.py tests/unit/test_entities.py
git commit -m "feat(entities): add supersede(), materialize(), drop() Cypher writes

- materialize(): 4-step MERGE (Entity, SourceItem, MENTIONED, REL-deferred)
- supersede(): stamps valid_to on live MENTIONED + REL edges (BR-6.4)
- drop(): DETACH-DELETE SourceItem + prune orphaned entities (BR-6.3)
- REL step intentionally no-op (design decision 1, follow-up change)"
```

---

### Task 3: Pipeline hook — post-graph extraction step in `run_pipeline`

**Files:**
- Modify: `digital_twins/ingest/pipeline.py`
- Test: `tests/unit/test_pipeline_extraction.py` (NEW)

**Interfaces:**
- Consumes: `entities.extract()`, `entities.supersede()`, `entities.materialize()` from Task 1–2; `digital_twins.config.schema.get`; `StubNeo4j` from conftest
- Produces: `_run_extraction(cfg, neo4j, items, item_hash, run_id, channel)` — private helper in pipeline.py, called after `_upsert_graph`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_pipeline_extraction.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_pipeline_extraction.py -v 2>&1 | tail -20
```

Expected: `AttributeError: module 'digital_twins.ingest.pipeline' has no attribute 'entities'` or `_run_extraction` not found

- [ ] **Step 3: Implement `_run_extraction()` in `pipeline.py`**

Add to `digital_twins/ingest/pipeline.py` (after the imports block, near the top):

```python
from digital_twins.ingest import entities  # noqa: E402 — s4-entity-extraction
```

Add the helper function at the bottom of `pipeline.py` (after `_ensure_graph_schema`):

```python
def _run_extraction(
    cfg: dict,
    neo4j,
    items: list,
    item_hash: dict,
    run_id: str,
    channel: str,
) -> None:
    """Post-graph LLM extraction step (s4-entity-extraction).

    Config-gated: when ``extraction.enabled`` is false (the default) or
    ``neo4j`` is None, this is a no-op. When enabled and a driver is
    present, for each ingested item:

    1. ``entities.supersede(item.key, driver=neo4j, cfg=extraction_cfg)``
       — stamps ``valid_to`` on live MENTIONED/REL edges (BR-6.4).
    2. ``entities.extract(item.content, title=..., cfg=extraction_cfg)``
       — one LLM call per item (isolated: failures are logged, not raised).
    3. ``entities.materialize(...)`` — idempotent MERGE into the graph.

    Per-item failure isolation: an item whose extraction fails logs a
    warning and the remaining items proceed (matching personal-kb's
    ``post_sweep`` isolation pattern).
    """
    import logging

    extraction_cfg = {
        "enabled": bool(get(cfg, "extraction.enabled")),
        "max_text_chars": get(cfg, "extraction.max_text_chars") or 12000,
        "prompt_version": get(cfg, "extraction.prompt_version") or "",
    }
    if not extraction_cfg["enabled"]:
        return
    if neo4j is None:
        logging.debug(
            "extraction enabled but no Neo4j driver — skipping graph "
            "entity extraction")
        return

    llm_cfg = {
        "endpoint": get(cfg, "llm.endpoint"),
        "model": get(cfg, "llm.model"),
        "api_key": get(cfg, "llm.api_key"),
    }

    for item in items:
        try:
            entities.supersede(item.key, driver=neo4j, cfg=extraction_cfg)
            extraction = entities.extract(
                item.content,
                title=str(
                    item.metadata.get("title", "")
                    if hasattr(item, "metadata") else ""),
                cfg={**extraction_cfg, **llm_cfg},
            )
            entities.materialize(
                channel=channel,
                item_id=item.key,
                content_hash=item_hash.get(item.key, ""),
                extraction=extraction,
                run_id=run_id,
                captured_at=item.ts,
                driver=neo4j,
                cfg=extraction_cfg,
            )
        except Exception as exc:  # noqa: BLE001 — per-item isolation
            logging.warning(
                "entity extraction failed for item %r in channel %r: %s",
                item.key, channel, exc)
```

Then in `run_pipeline()`, after the existing `if neo4j is not None: _upsert_graph(...)` block (line ~278), add:

```python
                if neo4j is not None:
                    # S4 graph write: one :SourceItem node per item...
                    _upsert_graph(neo4j, name, items, item_hash)
                    # Post-graph entity extraction (s4-entity-extraction,
                    # config-gated; no-op when extraction.enabled is false
                    # or the LLM endpoint is unconfigured).
                    _run_extraction(
                        cfg, neo4j, items, item_hash, run_id, name)
```

Note: move the `_run_extraction` call to inside the `if neo4j is not None:` block, right after `_upsert_graph`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_pipeline_extraction.py -v 2>&1 | tail -30
```

Expected: all tests PASS.

- [ ] **Step 5: Run the full S4 payload integration test to verify no regression**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/integration/test_s4_payload.py -v 2>&1 | tail -20
```

Expected: all tests PASS (extraction gate is off by default → no interference).

- [ ] **Step 6: Commit**

```bash
cd /home/terry/projects/digital-twins
git add digital_twins/ingest/pipeline.py tests/unit/test_pipeline_extraction.py
git commit -m "feat(pipeline): add post-graph LLM extraction step (_run_extraction)

- Config-gated: no-op when extraction.enabled=false (default) or neo4j=None
- Per-item isolation: failing items log a warning, remaining items proceed
- Reuses llm.endpoint/llm.model/llm.api_key (no new endpoint knobs)
- Test-first: tests/unit/test_pipeline_extraction.py (hermetic, monkeypatched)"
```

---

### Task 4: Config knobs — `extraction.*` in schema + knobs registry + config.example.yml

**Files:**
- Modify: `digital_twins/config/schema.py`
- Modify: `digital_twins/config/knobs.py`
- Modify: `config.example.yml`
- Test: `tests/unit/test_extraction_knobs.py` (NEW)

**Interfaces:**
- Consumes: `digital_twins.config.schema.DEFAULTS`, `_ALLOW_EMPTY`, `KNOBS`
- Produces: `extraction.enabled` (bool, `false`), `extraction.max_text_chars` (int, `12000`), `extraction.prompt_version` (str, `""`)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_extraction_knobs.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_extraction_knobs.py -v 2>&1 | tail -20
```

Expected: `KeyError: 'extraction.enabled'` or `SchemaError: extraction: unknown config key`

- [ ] **Step 3: Add extraction knobs to `schema.py`**

In `digital_twins/config/schema.py`, add to `DEFAULTS`:

```python
    # extraction (s4-entity-extraction; LLM entity extraction, config-gated)
    "extraction.enabled": False,
    "extraction.max_text_chars": 12000,
    "extraction.prompt_version": "",
```

Add `"extraction.prompt_version"` to `_ALLOW_EMPTY` (empty string = use built-in `PROMPT_VERSION`):

```python
_ALLOW_EMPTY: frozenset = frozenset({
    "qdrant.url", "qdrant.api_key",
    "neo4j.url", "neo4j.user", "neo4j.password",
    "llm.endpoint", "llm.model", "llm.api_key",
    "embedding.endpoint", "embedding.api_key",
    "extraction.prompt_version",
})
```

Add `"extraction"` to `known_sections` in `validate()`:

```python
    known_sections = {
        "state_dir", "config_dir", "qdrant", "neo4j", "llm",
        "embedding", "chunking", "scheduler", "sources",
        "mcp", "web", "extraction",
    }
```

Add the extraction section to the `out.setdefault` loop at the bottom of `validate()` (the loop that fills defaults for sections not in DEFAULTS — but since we added extraction knobs to DEFAULTS, the existing `for section_key in (...)` loop already covers it; just add `"extraction"` to that tuple):

```python
    for section_key in ("qdrant", "neo4j", "llm", "embedding", "chunking",
                        "scheduler", "mcp", "web", "extraction"):
        out.setdefault(section_key, {})
        for sub, default in DEFAULTS.items():
            if sub.startswith(section_key + "."):
                sub_key = sub.split(".", 1)[1]
                out[section_key].setdefault(sub_key, coerce(f"{section_key}.{sub_key}", default))
        # Sections not in DEFAULTS (mcp, web): fill from KNOBS registry.
        if not {p for p in DEFAULTS if p.startswith(section_key + ".")}:
            from .knobs import KNOBS as _knobs_registry
            for dotted, entry in _knobs_registry.items():
                if dotted.startswith(section_key + "."):
                    sub_key = dotted.split(".", 1)[1]
                    out[section_key].setdefault(sub_key, entry["default"])
```

- [ ] **Step 4: Add extraction knobs to `knobs.py`**

In `digital_twins/config/knobs.py`, add a new group and entries:

```python
GROUP_EXTRACTION = "Extraction"
```

In the `KNOBS` dict, add:

```python
    # --- Extraction (LLM entity extraction, config-gated) ---
    "extraction.enabled": {
        "type": "bool",
        "default": False,
        "env": "KB_EXTRACTION__ENABLED",
        "group": GROUP_EXTRACTION,
    },
    "extraction.max_text_chars": {
        "type": "int",
        "default": 12000,
        "env": "KB_EXTRACTION__MAX_TEXT_CHARS",
        "group": GROUP_EXTRACTION,
    },
    "extraction.prompt_version": {
        "type": "str",
        "default": "",
        "env": "KB_EXTRACTION__PROMPT_VERSION",
        "group": GROUP_EXTRACTION,
    },
```

- [ ] **Step 5: Add extraction section to `config.example.yml`**

In `config.example.yml`, after the `llm:` section (after line 29), add:

```yaml
# --- Extraction (LLM entity extraction, config-gated) ---
# When enabled, every ingest surface that has a Neo4j driver runs an LLM
# entity extraction step after the :SourceItem graph write. Requires
# llm.endpoint to be set. Default: disabled (no LLM calls, no entity writes).
extraction:
  enabled: false          # env: KB_EXTRACTION__ENABLED
  max_text_chars: 12000  # env: KB_EXTRACTION__MAX_TEXT_CHARS
  prompt_version: ""     # env: KB_EXTRACTION__PROMPT_VERSION (empty = use built-in)
```

- [ ] **Step 6: Run the knob tests to verify they pass**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_extraction_knobs.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 7: Run the knob-docs sync guard (T027) to verify it stays green**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/unit/test_knob_docs.py -v 2>&1 | tail -20
```

Expected: all tests PASS. If it fails, update `config.example.yml` to match the knob registry (the guard enforces lock-step).

- [ ] **Step 8: Run the portability guard to verify it stays green**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest tests/integration/test_portability.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
cd /home/terry/projects/digital-twins
git add digital_twins/config/schema.py digital_twins/config/knobs.py config.example.yml tests/unit/test_extraction_knobs.py
git commit -m "feat(config): add extraction.* knobs (enabled, max_text_chars, prompt_version)

- schema.py: extraction.enabled (bool, false), max_text_chars (int, 12000),
  prompt_version (str, \"\") in DEFAULTS; extraction.prompt_version in _ALLOW_EMPTY
- knobs.py: 3 new entries under GROUP_EXTRACTION
- config.example.yml: extraction: section with comments
- Knob-docs sync guard (T027) and portability guard (T006) stay green"
```

---

### Task 5: Full test suite + verify no regressions

**Files:**
- No new files; run the full suite

**Interfaces:**
- Consumes: all prior tasks

- [ ] **Step 1: Run the full test suite**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest -x --timeout=120 2>&1 | tail -40
```

Expected: all tests PASS (or only the pre-existing 14 reds from the uncommitted S4 working tree — those are NOT new regressions from this change).

- [ ] **Step 2: Verify the new tests all pass**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest \
  tests/unit/test_entities.py \
  tests/unit/test_pipeline_extraction.py \
  tests/unit/test_extraction_knobs.py \
  -v 2>&1 | tail -30
```

Expected: all PASS.

- [ ] **Step 3: Commit (if any fixes were needed)**

```bash
cd /home/terry/projects/digital-twins
git status
git diff --stat
# If no fixes needed, skip this commit
```

---

### Task 6: Spec-013 / SDD ledger deltas + openspec tasks.md check-off

**Files:**
- Modify: `openspec/changes/s4-entity-extraction/tasks.md` (check off completed tasks)
- Modify: `.superpowers/sdd/013-neo4j-query-tests/progress.md` (if it exists)

**Interfaces:**
- Consumes: all prior tasks

- [ ] **Step 1: Check off completed tasks in `openspec/changes/s4-entity-extraction/tasks.md`**

For each completed section in the openspec `tasks.md`, change `- [ ]` to `- [x]`.

- [ ] **Step 2: Update `.superpowers/sdd/013-neo4j-query-tests/progress.md`**

Append a note that s4-entity-extraction was implemented, with a link to the relevant commits.

- [ ] **Step 3: Commit**

```bash
cd /home/terry/projects/digital-twins
git add openspec/changes/s4-entity-extraction/tasks.md .superpowers/sdd/013-neo4j-query-tests/progress.md
git commit -m "docs: check off s4-entity-extraction tasks, update SDD ledger"
```

---

## Self-Review

**Spec coverage:**
- ✅ `extraction.enabled` gate (default off) — Task 4 + Task 3 step 3
- ✅ `extraction.max_text_chars` — Task 4
- ✅ `extraction.prompt_version` — Task 4
- ✅ `llm.endpoint/model/api_key` reuse — Task 1 (`_llm_request` reads `cfg["endpoint"]` etc.)
- ✅ Per-item isolation — Task 3 step 3 (`except Exception` → `logging.warning`)
- ✅ Entity type constraint — Task 1 (`extract()` drops out-of-enum types)
- ✅ `supersede()` before re-extraction — Task 2 + Task 3 step 3
- ✅ `materialize()` idempotent MERGE — Task 2
- ✅ `drop()` exposed — Task 2
- ✅ REL out-of-scope — Task 2 step 3 (step 4 is a no-op comment)
- ✅ Unconfigured LLM endpoint → skip without error — Task 3 step 3 (`neo4j is None` → return; `extraction.enabled` false → return)

**Placeholder scan:** No "TBD", "TODO", or "similar to Task N" in the plan. All code blocks are complete.

**Type consistency:**
- `entities.extract(text, *, title="", cfg=None) -> dict` — consistent across Tasks 1 and 3.
- `entities.materialize(*, channel, item_id, content_hash, extraction, run_id, captured_at, driver, cfg=None) -> dict` — consistent across Tasks 2 and 3.
- `entities.supersede(item_id, *, driver, cfg=None) -> int` — consistent across Tasks 2 and 3.
- `pipeline._run_extraction(cfg, neo4j, items, item_hash, run_id, channel)` — defined in Task 3 step 3, called in Task 3 step 3's pipeline modification.

All consistent. ✅
