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

### Requirement: Extraction is config-gated (default off)
When `extraction.enabled` is false (the default), or no Neo4j driver is
available, the extraction step is skipped entirely — no LLM call, no
entity or MENTIONED writes beyond the `:SourceItem` node. When true,
extraction runs per ingested item with per-item failure isolation: an
item whose extraction fails logs a warning and the remaining items
proceed.

#### Scenario: Gate off, no LLM call
- **WHEN** `extraction.enabled` is false
- **THEN** no LLM request is made and no `:Entity`/`MENTIONED` writes occur.

#### Scenario: Per-item isolation on LLM failure
- **WHEN** extraction is enabled and the LLM call for item N fails
- **THEN** item N logs a warning and items N+1..N+K proceed normally.

### Requirement: Entity types are constrained
LLM-extracted entities shall have `type` in the `ENTITY_TYPES` tuple
(`person` / `organization` / `place` / `event` / `concept`). Entities
deduplicated by `(name.lower(), type)` and capped at 25 entities /
100 relations per item.

#### Scenario: Out-of-enum type discarded
- **WHEN** the LLM returns an entity with `type: "company"` (not in
  `ENTITY_TYPES`)
- **THEN** that entity is discarded and not written to Neo4j.

### Requirement: Extraction reuses the LLM endpoint
Extraction uses the configured `llm.endpoint` / `llm.model` /
`llm.api_key` knobs (no extraction-specific endpoint knobs). An unconfigured
endpoint is treated as extraction-unavailable (the step is skipped, same
as gate-off).

#### Scenario: Unconfigured LLM endpoint
- **WHEN** `extraction.enabled` is true but `llm.endpoint` is empty
- **THEN** extraction is skipped without error and the ingest run
  completes normally.

## Non-goals
- No `REL` edge generation in this change (REL edges are scoped to a single
  item via `source_item` in personal-kb's `materialize()`; they never cross
  items/channels, so "cross-channel REL stays personal-kb-only" is already
  true in personal-kb, not a digital-twins restriction. If REL production
  from digital-twins content is needed, a follow-up change would extend the
  extractor to emit relations).
