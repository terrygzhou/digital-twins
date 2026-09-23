# Capability: ingestion — S4 entity extraction (delta)

## ADDED Requirements

### Requirement: LLM entity extraction writes S4 Entity/MENTIONED nodes
When extraction is enabled, ingesting an item shall produce `(:Entity)`
nodes (type in person/organization/place/event/concept) and
`(:SourceItem)-[:MENTIONED {run_id, valid_from, valid_to, prompt_version,
captured_at}]->(:Entity)` edges, idempotently per (item, prompt_version).

#### Scenario: Re-extraction time-boxes
- **WHEN** an item is re-extracted with the same prompt_version
- **THEN** previous `MENTIONED` edges are superseded (`valid_to` stamped)
  and new edges are written with `valid_to = NULL`.

## Non-goals
- No `REL` edge generation in this change (REL edges are scoped to a single
  item via `source_item` in personal-kb's `materialize()`; they never cross
  items/channels, so "cross-channel REL stays personal-kb-only" is already
  true in personal-kb, not a digital-twins restriction. If REL production
  from digital-twins content is needed, a follow-up change would extend the
  extractor to emit relations).
