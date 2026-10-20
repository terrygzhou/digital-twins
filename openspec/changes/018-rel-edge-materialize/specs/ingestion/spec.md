## Purpose
Behavioural contract for REL edge production in `entities.materialize()`:
a genuine entity↔entity edge is written to Neo4j for each LLM-parsed
relation, gated by `extraction.enabled` and a live Neo4j driver.

## ADDED Requirements

### Requirement: REL edges are materialized from parsed relations
When `extraction.enabled` is `true` and a Neo4j driver is present,
`materialize()` SHALL MERGE one `REL` edge per relation in the parsed
extraction result:

```cypher
MATCH (e1:Entity {name: $subject, type: $subject_type})
MATCH (e2:Entity {name: $object,   type: $object_type})
MERGE (e1)-[r:REL {predicate: $predicate, source_item: $item_id,
                    prompt_version: $pv}]->(e2)
ON CREATE SET r.valid_from = $now
ON MATCH  SET r.valid_to = NULL, r.last_seen = $now, r.run_id = $run_id
```

The edge SHALL carry `source_item = item_id` so RELs are scoped to the
item that produced them (never cross-channel, never cross-item).

#### Scenario: Fresh extraction writes REL edges
- **WHEN** `materialize()` is called for an extraction with 2 relations
- **THEN** the Neo4j query
  `MATCH (e1:Entity)-[r:REL]->(e2:Entity) WHERE r.source_item = $item_id
   RETURN count(r)`
  returns 2

#### Scenario: Idempotent re-run writes no new REL edges
- **WHEN** `materialize()` is called twice for the same extraction
  (identical `prompt_version`, same relations)
- **THEN** the second call's REL write counter is 0 (no duplicate
  edges, NFR-1)

### Requirement: REL production is gated by extraction.enabled
When `extraction.enabled` is `false` (the default) or no Neo4j driver
is available, `materialize()` SHALL NOT write REL edges; its return
`"relations"` field SHALL be 0.

#### Scenario: Default off → no REL writes
- **WHEN** a fresh deployment runs `kb_ingest` with no
  `extraction.enabled` override
- **THEN** the extraction step is skipped entirely and no `REL` edges
  are created in Neo4j
