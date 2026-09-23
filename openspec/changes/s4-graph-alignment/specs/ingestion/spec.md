# Capability: ingestion — S4 graph/payload alignment (delta)

## ADDED Requirements

### Requirement: Qdrant points carry S4-compatible payload fields
Every chunk point written by digital-twins shall include the fields
`item_id`, `content_hash`, `full_content`, `content_snippet`,
`captured_at`, `source_url`, `source_title`, `total_chunks`,
`embed_model`, `source_type`, `tags`, and a `meta` object (multi-user
`owner`/`owner_tag` fields shall live under `meta`, not at top level).
Provenance fields `run_id`/`trigger` shall NOT be treated as contract
fields; when present they must equal the audit row's `run_id`/`trigger`
for that run. `content_hash` shall be
computed from the item's full text and be identical across all chunks of
that item. `item_id` is the single join key between Qdrant and Neo4j —
it equals the `SourceItem.item_id` node property and the source's
`item.key`.

#### Scenario: Payload contract on ingest
- **WHEN** an item with N chunks is ingested via any trigger
  (schedule, run, mcp, web)
- **THEN** each of the N points carries `item_id` equal to the item's
  stable key, `total_chunks` equal to N, one shared item-level
  `content_hash`, and all required fields above; `meta` carries any
  owner fields; any present `run_id`/`trigger` equals the audit row's
  values for that run.

### Requirement: Neo4j writes the S4 SourceItem node
Ingest with Neo4j enabled shall write one `(:SourceItem {item_id, channel,
content_hash})` node per ingested item (idempotent MERGE on `item_id`),
and shall no longer write `KbItem`, `KbChunk`, or `HAS_CHUNK`.

#### Scenario: Re-ingest is idempotent
- **WHEN** the same item is ingested twice
- **THEN** exactly one `:SourceItem` node exists for its `item_id`, and
  node counts do not grow between runs.

### Requirement: Content-independent deterministic point IDs
Point IDs shall be `uuid5(NAMESPACE_DNS, "kb:{channel}:{item_id}:{chunk_index}")`
— independent of content — so re-ingesting changed content upserts the same
points in place.

#### Scenario: Content change re-ingest
- **WHEN** an item whose text changed is re-ingested
- **THEN** the same point IDs are upserted and no new points are minted.

## MODIFIED Requirements

### Requirement: Legacy graph labels are removable
A migration step shall delete all legacy `KbItem` and `KbChunk` nodes
idempotently, reporting counts in `--dry-run` mode and performing the
deletion when run for real; it shall be a no-op on databases that never
wrote the legacy shape.

#### Scenario: Migration on fresh database
- **WHEN** `digital-twins migrate s4` runs against a database without
  legacy labels
- **THEN** it completes successfully, reporting 0 nodes deleted.

#### Scenario: Migration on legacy database
- **WHEN** `digital-twins migrate s4 --dry-run` runs against a database
  containing `KbItem`/`KbChunk` nodes
- **THEN** it reports the node counts without deleting; a subsequent
  real run deletes all `KbItem`/`KbChunk` nodes and leaves
  `:SourceItem` untouched.
