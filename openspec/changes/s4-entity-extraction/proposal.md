# Change: S4 entity extraction (follow-up, proposed)

## Why
After s4-graph-alignment, digital-twins items participate in S4 hybrid
search but have no `Entity`/`MENTIONED`/`REL` edges, so graph expansion
(`_expand_items`) returns empty sets for digital-twins content. Full S4
parity requires an LLM extraction step matching personal-kb's contract
(`PROMPT_VERSION`, `valid_from`/`valid_to` time-boxing, `supersede` on
re-extraction, `drop` on reconcile).

## What changes
- New `digital_twins/ingest/entities.py`: LLM extraction of
  `Entity {name, type, desc}` (ENTITY_TYPES: person/organization/place/
  event/concept, with `product`/`url` planned for a future prompt version
  bump) + `MENTIONED` edges from `SourceItem`, prompt versioned.
- `supersede()` stamps `valid_to` on re-extraction; `drop()` DETACH-DELETes
  orphaned entities on reconcile (BR-6.3/6.4 parity).
- LLM access is config-gated (new knob, default off); no dependency
  bundled — uses the configured endpoint (SGLang/OpenAI-compatible).
- **REL edges are out of scope for this change** (they require cross-entity
  inference that personal-kb's `materialize()` already handles; a third
  change would add digital-twins' REL production if needed).

## Impact
- Affected code: new `digital_twins/ingest/entities.py`, pipeline hook
  after `_upsert_graph`, config knobs + docs, spec-013 deltas.
- New config: `extraction.enabled` (bool, default false),
  `extraction.max_text_chars` (int, default 12000),
  `extraction.prompt_version` (str, default built-in).
  Extraction reuses the `llm.endpoint` / `llm.model` / `llm.api_key`
  knobs (no new endpoint knobs — a single LLM endpoint serves all uses
  in one deployment).

## Non-goals
- No change to personal-kb; no REL edge generation in this change.
  personal-kb's `REL` edges are already scoped to a single item via
  `source_item` (they never cross items/channels), so "cross-channel REL
  stays personal-kb-only" is not a restriction digital-twins is choosing
  to impose — it is already true in personal-kb. This change deliberately
  adds no REL production; if RELs are needed from digital-twins content,
  a follow-up change would extend the extractor to emit relations.
- No synchronous extraction in the MCP fast path (async/batched only).
