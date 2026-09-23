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
  event/concept) + `MENTIONED` edges from `SourceItem`, prompt versioned.
- `supersede()` stamps `valid_to` on re-extraction; `drop()` DETACH-DELETes
  orphaned entities on reconcile (BR-6.3/6.4 parity).
- LLM access is config-gated (new knob, default off); no dependency
  bundled — uses the configured endpoint (SGLang/OpenAI-compatible).

## Impact
- Affected code: new `digital_twins/ingest/entities.py`, pipeline hook
  after `_upsert_graph`, config knobs + docs, spec-013 deltas.
- New config: `extraction.enabled`, `extraction.endpoint`,
  `extraction.model`, `extraction.prompt_version`.

## Non-goals
- No change to personal-kb; no `REL` edge inference between entities from
  different channels (cross-channel REL stays personal-kb-only).
- No synchronous extraction in the MCP fast path (async/batched only).
