# Design: S4 entity extraction (LLM)

## Context

personal-kb's S4 graph half (`kb/core/graph.py`) runs an LLM extraction step
per ingested item, then materialises the result into Neo4j:

- `EXTRACTION_PROMPT` — versioned constant (`PROMPT_VERSION = "2026-08-23.1"`),
  pinned overridable via `graph.prompt_version` in kb.yml / `KB_GRAPH_PROMPT_VERSION`.
- `ENTITY_TYPES = ("person", "organization", "place", "event", "concept")`
  — type-constrained; anything else is dropped from the extraction.
- LLM transport: stdlib `urllib`, SGLang `/v1/chat/completions` JSON mode,
  `temperature=0.0`, `max_tokens=3000`, bearer key from `llm.api_key_env`
  (default `SGLANG_API_KEY`).
- `extract(text, title, cfg)` → `{"entities": [...], "relations": [...],
  "prompt_version": str}` — deduped, type-constrained, capped at 25 entities
  / 100 relations.
- `materialize(channel, item_id, content_hash, extraction, run_id,
  captured_at, driver)` — idempotent MERGE writes:
  1. `(:Entity {name, type, created, desc, last_seen})` per unique entity
  2. `(:SourceItem {item_id, channel, content_hash, last_run_id, last_captured_at})`
  3. `(:SourceItem)-[:MENTIONED {run_id, prompt_version, valid_from, valid_to,
     last_seen, captured_at}]->(:Entity)` per entity; `ON MATCH` revives
     superseded edges (`valid_to` cleared)
  4. `(:Entity)-[:REL {predicate, source_item, run_id, prompt_version,
     valid_from, valid_to, last_seen}]->(:Entity)` per relation; `ON MATCH`
     revives; REL edges are attributed to the source item via `source_item`
     so supersede/drop can scope them.
- `supersede(item_id, driver)` — before re-extraction, sets `valid_to` on the
  item's live `MENTIONED` + `REL` edges (BR-6.4).
- `drop(item_id, driver)` — after reconcile delete, DETACH-DELETEs the
  item's `:SourceItem` and prunes entities that lost their last reference
  (BR-6.3).
- `post_sweep(ingested, driver, cfg)` — batch: runs over at most
  `graph.max_items_per_run` items; every per-item failure is isolated into
  a note (identical isolation pattern to reconcile), never raised.

digital-twins today (`s4-graph-alignment`): writes `:SourceItem` nodes only.
The graph arm in personal-kb's `hybrid.py` (`_entities_of_items` →
`_expand_items`) walks `SourceItem-[MENTIONED]->Entity` and returns empty
sets for digital-twins content.

## What changes in digital-twins

New module `digital_twins/ingest/entities.py`, mirroring personal-kb's
`kb/core/graph.py` contract:

### Prompt + extraction

- `PROMPT_VERSION` constant (initial value matches personal-kb's current
  `PROMPT_VERSION`; bump on prompt or entity-type set change).
- `ENTITY_TYPES = ("person", "organization", "place", "event", "concept")`
  — same set as personal-kb; the prompt is constrained to this set and so is
  materialisation (anything else is dropped).
- `extract(text: str, *, title: str = "", cfg: dict) -> dict`
  → `{"entities": [...], "relations": [...], "prompt_version": str}`.
  Same shape, dedup, caps (25 entities / 100 relations), and type
  constraint as personal-kb's `extract`.
- LLM transport: reuse the **existing** `llm.endpoint` / `llm.model` /
  `llm.api_key` knobs (the SGLang/OpenAI-compatible endpoint personal-kb
  already uses); `api_key` read from the config layer, not an env var (the
  config layer's `llm.api_key` knob is the canonical surface; the personal-kb
  env-var pattern is host-specific and not portable — BR-11.2 / NFR-13).
  When `extraction.enabled` is false (default) the LLM is never called.

### Materialisation

- `materialize(channel, item_id, content_hash, extraction, run_id,
  captured_at, driver, cfg) -> dict` — same four-step MERGE sequence as
  personal-kb's `materialize`; returns `{"entities": N, "mentioned": N,
  "relations": N}` counters.
- `supersede(item_id, driver) -> int` — same two-statement Cypher as
  personal-kb.
- `drop(item_id, driver) -> bool` — same DETACH-DELETE + entity-prune
  Cypher as personal-kb.

### Pipeline hook

`run_pipeline` gains an optional post-graph step (config-gated, default off):

```
if extraction.enabled and neo4j is not None:
    for item in ingested_items:
        text = item.content[:extraction.max_text_chars]   # default 12000
        ex = entities.extract(text, title=item.title, cfg=cfg)
        entities.supersede(item.key, driver=neo4j)
        entities.materialize(
            channel=name, item_id=item.key,
            content_hash=item_hash[item.key],
            extraction=ex, run_id=run_id,
            captured_at=item.ts, driver=neo4j, cfg=cfg)
```

Per-item failures are isolated (logged, not raised) — matching personal-kb's
`post_sweep` isolation pattern and the pipeline's existing fail-soft
semantics for optional steps. The `:SourceItem` node is already written by
`_upsert_graph` before this step, so `materialize`'s MERGE on
`SourceItem {item_id}` is a no-op update.

### Config knobs

| Knob | Type | Default | Purpose |
|---|---|---|---|
| `extraction.enabled` | bool | `false` | Gate: LLM extraction off unless explicitly enabled |
| `extraction.max_text_chars` | int | `12000` | Truncation cap fed to the prompt (same as personal-kb) |
| `extraction.prompt_version` | str | `""` (→ built-in `PROMPT_VERSION`) | Override the pinned version |

Reuses existing knobs (no new endpoint/model knob — the LLM endpoint is the
same `llm.*` surface personal-kb uses, so a single deployment configures both):

| Knob | Type | Default | Purpose |
|---|---|---|---|
| `llm.endpoint` | str | `""` | SGLang / OpenAI-compatible base URL |
| `llm.model` | str | `""` | Model name (`""` = served default) |
| `llm.api_key` | str | `""` | Bearer token (config layer, not env var) |

## Recorded decisions

1. **REL edges: out of scope.** personal-kb's `materialize()` already
   handles REL production (entity-to-entity edges scoped to a single item
   via `source_item`). digital-twins' extractor in this change writes
   `:Entity` + `:MENTIONED` only; REL production is a separate follow-up
   change if needed. The `extract()` function still parses `relations` from
   the LLM response (the prompt requests them), but `materialize()` in this
   change does not write REL edges — keeping the graph shape minimal and
   matching personal-kb's write-side split (graph.py writes both; this
   change deliberately scopes to Entity/MENTIONED only, with REL deferred).

   *Revisit trigger:* if personal-kb's query layer or a downstream consumer
   expects digital-twins content to have REL edges, open a follow-up
   change to extend `materialize()`.

2. **Prompt version stays in sync with personal-kb.** The initial
   `PROMPT_VERSION` value and the prompt text are copied from personal-kb's
   `EXTRACTION_PROMPT` / `PROMPT_VERSION`. If personal-kb bumps its version
   (prompt change, entity-type set change), digital-twins must re-extract
   (supersede + re-materialise) to stay in the same version space. The
   `extraction.prompt_version` override knob allows pinning a specific
   version for a deployment without a code change.

3. **No new LLM dependency.** The stdlib `urllib` transport (SGLang
   OpenAI-compatible `/v1/chat/completions`, JSON mode) is the same
   transport personal-kb uses. No `openai` or other SDK is added (BR-11 /
   NFR-13: no new host paths or dependencies).

4. **Per-item isolation, not batch isolation.** personal-kb runs
   extraction in a post-sweep batch (`post_sweep`, capped at
   `graph.max_items_per_run`). digital-twins integrates extraction
   inline in `run_pipeline` (one LLM call per item, per chunk batch). The
   per-item failure isolation is the same: a failed LLM call or malformed
   JSON logs a warning and skips that item's graph writes; the pipeline
   continues with the remaining items. No new cap knob in this change;
   the item count is bounded by `sources.<name>.max_items`.

5. **No reconcile path in this change.** `drop()` is exposed for future
   reconcile use (BR-6.3 parity), but digital-twins has no reconcile
   mechanism yet — `drop` is a no-op until a reconcile change adds the
   Qdrant-point-delete + graph-drop coordination. The function is
   available for the future reconcile change to call.

## Open questions

1. **Extraction trigger surface:** extraction runs on the MCP
   (`kb_ingest`) and web ingest surfaces in addition to schedule and CLI
   `--once` — every surface where Neo4j is configured and
   `extraction.enabled` is true, matching the "every ingest surface writes
   the graph" decision in s4-graph-alignment. The MCP fast path
   (`kb_ingest`) and web ingest call the same `run_pipeline`, so
   extraction is synchronous there too (bounded by the item's text
   length, not by a batch cap). *Revisit trigger:* if LLM latency becomes
   a problem on the MCP surface, add an `extraction.defer_mcp` knob to
   defer extraction to the next schedule tick.

2. **Entity type set completeness:** start with personal-kb's
   `person/organization/place/event/concept` (matching `ENTITY_TYPES`).
   Plan to add `product` and `url` in a future prompt version bump
   (requires re-extraction across both systems). *Revisit trigger:* first
   user report of a missing entity type.

3. **LLM timeout / retry:** keep personal-kb's fixed 120 s timeout with
   no retry. *Revisit trigger:* if extraction failures correlate with LLM
   latency, add a retry knob.
