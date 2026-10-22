"""LLM entity extraction + Neo4j materialisation (S4 entity half).

Mirrors personal-kb's ``kb/core/graph.py`` contract:

- ``PROMPT_VERSION`` — pinned generic prompt version (bump = re-extract).
- ``ENTITY_TYPES`` — allowed entity types for the generic profile.
- ``DOMAIN_ENTITY_TYPES`` / ``DOMAIN_PREDICATES`` — Tier B domain profile
  (020-domain-extraction-schema): entity types + predicate allowlist.
- ``extract()`` — LLM call → ``{"entities", "relations", "prompt_version"}``.
  With ``cfg={"domain_schema": True}`` the domain prompt / types /
  predicate allowlist apply and ``DOMAIN_PROMPT_VERSION`` is stamped.
- ``supersede()`` — stamps ``valid_to`` on live MENTIONED edges after a
  successful re-extract, before materialization (BR-6.4 parity; 019b:
  supersede-after-extract, idempotent-by-default on failure).
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
import urllib.request
from typing import Any
from datetime import UTC, datetime

__all__ = [
    "PROMPT_VERSION",
    "ENTITY_TYPES",
    "DOMAIN_ENTITY_TYPES",
    "DOMAIN_PREDICATES",
    "DOMAIN_PROMPT_VERSION",
    "DOMAIN_EXTRACTION_PROMPT",
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

#: Domain profile (Tier B, 020-domain-extraction-schema): allowed entity
#: types for domain-aware extraction. The domain prompt is constrained to
#: this set, and materialisation drops anything else.
DOMAIN_ENTITY_TYPES = ("product", "customer", "competitor", "standard", "cert")

#: Domain profile (Tier B): the relation-predicate allowlist. In domain mode
#: relations whose lower-cased predicate is not in this set are dropped
#: (no free-form predicates); the generic profile is unaffected.
DOMAIN_PREDICATES = (
    "competes_with",
    "supplied_to",
    "complies_with",
    "substitutes_for",
    "manufactured_by",
    "tested_to",
)

#: Domain prompt version — stamped on domain-profile extractions (bump =
#: re-extract). Distinct from PROMPT_VERSION so the two profiles stamp
#: different edges.
DOMAIN_PROMPT_VERSION = "2026-08-23.2"

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

#: Domain extraction prompt (Tier B, 020-domain-extraction-schema).
#: Same shape as EXTRACTION_PROMPT but constrained to the domain entity
#: types and the fixed predicate allowlist (no free-form predicates).
DOMAIN_EXTRACTION_PROMPT = f"""\
You are an entity-extraction pipeline for a domain knowledge base.
Read the document and extract the entities and relations it contains.

Rules:
- "entities": domain entities — products, customers, competitors, standards
  and certifications that are concrete enough to be useful for navigation.
  3 to 25 entities.
- Each entity: {{"name": str, "type": one of product|customer|competitor|standard|cert,
  "desc": short description, max 120 chars}}.
- "relations": only relations explicitly supported by the document, each
  {{"subject": entity name, "predicate": one of
  competes_with | supplied_to | complies_with | substitutes_for |
  manufactured_by | tested_to,
  "object": entity name}}. subject and object MUST be names from "entities".
- Do not invent entities or relations that are not in the document.
- If the document is too thin to extract anything useful, return
  {{"entities": [], "relations": []}}.

Respond with JSON only, in exactly this shape:
 {{"entities": [{{"name": ..., "type": ..., "desc": ...}}],
   "relations": [{{"subject": ..., "predicate": ..., "object": ...}}]}}
 (prompt version {DOMAIN_PROMPT_VERSION})
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
    """Effective prompt version: pinned config > built-in.

    In domain mode the built-in default is :data:`DOMAIN_PROMPT_VERSION`
    (a pinned override still wins).
    """
    cfg = cfg or {}
    pinned = str(cfg.get("prompt_version", "")).strip()
    return pinned or (
        DOMAIN_PROMPT_VERSION if cfg.get("domain_schema") else PROMPT_VERSION)


def _profile(cfg: dict | None) -> str:
    """Extraction profile for a cfg dict: ``"domain"`` when
    ``cfg.get("domain_schema")`` is truthy, else ``"generic"`` (020, Tier B).
    One profile knob selects prompt + type set + predicate filter atomically.
    """
    return "domain" if (cfg or {}).get("domain_schema") else "generic"


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
        # llm.endpoint is the OpenAI-compatible base incl. /v1 (matches
        # llm.py + health preflight which probe {endpoint}/models); do NOT
        # re-prepend /v1 here or the request 404s at .../v1/v1/chat/completions.
        f"{endpoint}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (api_key.strip() or "none"),
        },
        method="POST",
    )
    # 5xx retry-once (house style, mirrors embedding.py / health.py): a
    # transient server error is retried exactly once; timeouts and
    # non-5xx errors are not retried (deterministic failure, surfaced as
    # a per-item extraction failure).
    try:
        import urllib.error as _urlerror
        data = None
        for _attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                break
            except _urlerror.HTTPError as he:
                if 500 <= he.code < 600 and _attempt == 1:
                    # 5xx on attempt 1 -> one retry; attempt-2 5xx or any
                    # non-5xx falls through to the raise below.
                    continue
                raise
        if data is None:
            raise _urlerror.URLError("no response after 5xx retry")
    except Exception as e:  # noqa: BLE001 - surfaced as a per-item failure
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
    with types constrained to :data:`ENTITY_TYPES` (or to
    :data:`DOMAIN_ENTITY_TYPES` in domain mode). Raises
    :class:`ExtractionError` on LLM/parse failure — callers isolate per item.

    Domain mode (``cfg={"domain_schema": True}``): uses
    :data:`DOMAIN_EXTRACTION_PROMPT`, constrains entity types to
    :data:`DOMAIN_ENTITY_TYPES`, applies the :data:`DOMAIN_PREDICATES`
    allowlist to relations, and stamps :data:`DOMAIN_PROMPT_VERSION`.
    """
    cfg = cfg or {}
    domain_mode = _profile(cfg) == "domain"
    allowed_types = DOMAIN_ENTITY_TYPES if domain_mode else ENTITY_TYPES
    prompt = DOMAIN_EXTRACTION_PROMPT if domain_mode else EXTRACTION_PROMPT
    max_chars = _safe_int(cfg.get("max_text_chars"), 12000)
    body = text[:max_chars]
    user = (f"Title: {title}\n\n" if title else "") + body
    content = _llm_request(
        [
            {"role": "system", "content": prompt},
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
        if not name or etype not in allowed_types:
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
        if domain_mode and pred not in DOMAIN_PREDICATES:
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


def _entity_type(entities: list[dict], name: str) -> str:
    """Look up an entity's type by name from the extraction's entity list."""
    for e in entities:
        if e.get("name", "") == name:
            return e.get("type", "")
    return ""


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
    """Idempotent MERGE of one extraction's entities + MENTIONED + REL edges.

    Returns ``{"entities": N, "mentioned": N, "relations": M}`` where N and
    M are the non-zero write counters (0 on a no-op re-run). 018: the REL
    step MERGEs one edge per LLM-parsed relation, mirroring the MENTIONED
    step (ON-CREATE stamps valid_from/captured_at/run_id; ON-MATCH clears
    valid_to, bumps last_seen/run_id).
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

    # Step 4: per-relation REL MERGE (018-rel-edge-materialize)
    relations = extraction.get("relations") or []
    relations_written = 0
    for rel in relations:
        subj = rel.get("subject", "")
        pred = rel.get("predicate", "")
        obj = rel.get("object", "")
        relations_written += _run(
            driver,
            "MATCH (e1:Entity {name: $subject, type: $subject_type}) "
            "MATCH (e2:Entity {name: $object, type: $object_type}) "
            "MERGE (e1)-[r:REL {predicate: $predicate, "
            "source_item: $item_id, prompt_version: $pv}]->(e2) "
            "ON CREATE SET r.valid_from = $now, "
            "r.captured_at = $captured_at, r.run_id = $run_id "
            "ON MATCH SET r.valid_to = NULL, r.last_seen = $now, "
            "r.run_id = $run_id",
            subject=subj, subject_type=_entity_type(entities, subj),
            object=obj, object_type=_entity_type(entities, obj),
            predicate=pred, item_id=item_id,
            pv=pv, now=now,
            captured_at=captured_at, run_id=run_id,
        )

    return {
        "entities": entities_written,
        "mentioned": mentioned_written,
        "relations": relations_written,
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
