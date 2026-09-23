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


# ── Neo4j materialisation ────────────────────────────────────────────────────


def _now() -> str:
    """UTC ISO-8601 timestamp for edge/node bookkeeping fields."""
    return datetime.now(UTC).isoformat()


def _run(driver, cypher: str, **params) -> int:
    """Run one write Cypher; return a non-zero counter when the query
    actually wrote anything. Tolerates the stub driver (no SummaryCounters)."""
    with driver.session() as s:
        result = s.run(cypher, **params)
        try:
            summary = result.consume()
            c = summary.counters
            return (
                _safe_int(c.nodes_created, 0)
                + _safe_int(c.nodes_deleted, 0)
                + _safe_int(c.relationships_created, 0)
                + _safe_int(c.relationships_deleted, 0)
                + _safe_int(c.properties_set, 0)
            )
        except AttributeError:
            # Stub driver: no SummaryCounters — treat as 0 updates.
            return 0


def supersede(item_id: str, *, driver, cfg: dict | None = None) -> int:
    """Stamp ``valid_to`` on all live MENTIONED + REL edges owned by
    ``item_id`` so re-extraction re-issues them (BR-6.4)."""
    now = _now()
    # MENTIONED supersede
    n1 = _run(
        driver,
        "MATCH (si:SourceItem {item_id: $item_id})-[m:MENTIONED]->() "
        "WHERE m.valid_to IS NULL "
        "SET m.valid_to = $now",
        item_id=item_id, now=now,
    )
    # REL supersede (scoping by source_item)
    n2 = _run(
        driver,
        "MATCH ()-[r:REL]->() "
        "WHERE r.source_item = $item_id AND r.valid_to IS NULL "
        "SET r.valid_to = $now",
        item_id=item_id, now=now,
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
    """Idempotent MERGE of one extraction's entities + MENTIONED edges (NFR-1).

    Returns ``{"entities": N, "mentioned": N, "relations": 0}`` where N is
    the non-zero write counter (0 on a no-op re-run). REL step is a no-op
    (design decision 1 — REL production is a follow-up change).
    """
    now = _now()
    entities = extraction.get("entities") or []
    pv = extraction.get("prompt_version") or PROMPT_VERSION
    entities_written = 0
    mentioned_written = 0

    # Step 1: per-entity MERGE
    for ent in entities:
        name = ent.get("name", "")
        etype = ent.get("type", "")
        desc = ent.get("desc", "")
        entities_written += _run(
            driver,
            "MERGE (e:Entity {name: $name, type: $type}) "
            "ON CREATE SET e.created = $now, e.desc = $desc "
            "ON MATCH SET e.last_seen = $now, "
            "e.desc = CASE WHEN $desc <> '' THEN $desc ELSE e.desc END",
            name=name, type=etype, desc=desc, now=now,
        )

    # Step 2: SourceItem MERGE
    _run(
        driver,
        "MERGE (si:SourceItem {item_id: $item_id}) "
        "SET si.channel = $channel, si.content_hash = $content_hash, "
        "si.last_run_id = $run_id, si.last_captured_at = $captured_at",
        item_id=item_id, channel=channel,
        content_hash=content_hash, run_id=run_id,
        captured_at=captured_at,
    )

    # Step 3: per-entity MENTIONED
    for ent in entities:
        name = ent.get("name", "")
        etype = ent.get("type", "")
        mentioned_written += _run(
            driver,
            "MATCH (si:SourceItem {item_id: $item_id}) "
            "MATCH (e:Entity {name: $name, type: $type}) "
            "MERGE (si)-[m:MENTIONED]->(e) "
            "ON CREATE SET m.captured_at = $captured_at, m.run_id = $run_id, "
            "m.prompt_version = $pv, m.valid_from = $now "
            "ON MATCH SET m.valid_to = NULL, m.last_seen = $now, "
            "m.run_id = $run_id",
            item_id=item_id, name=name, type=etype,
            captured_at=captured_at, run_id=run_id,
            pv=pv, now=now,
        )

    # Step 4: REL — intentionally no-op (design decision 1, follow-up change)
    # relations_written = 0

    return {
        "entities": entities_written,
        "mentioned": mentioned_written,
        "relations": 0,
    }


def drop(item_id: str, *, driver, cfg: dict | None = None) -> bool:
    """DETACH-DELETE a SourceItem and prune orphaned entities (BR-6.3).

    Returns True when a SourceItem was actually deleted.
    """
    with driver.session() as s:
        result = s.run(
            "MATCH (si:SourceItem {item_id: $item_id}) "
            "DETACH DELETE si RETURN count(si) AS deleted",
            item_id=item_id,
        )
        record = result.single()
        deleted = int(record["deleted"]) if record else 0
        if deleted > 0:
            # Prune entities no longer referenced by any SourceItem
            _run(
                driver,
                "MATCH (e:Entity) "
                "WHERE NOT (e)<-[:MENTIONED]-(:SourceItem) "
                "AND NOT (e)-[:REL]->() AND NOT (:Entity)-[:REL]->(e) "
                "DELETE e",
            )
    return bool(deleted)
