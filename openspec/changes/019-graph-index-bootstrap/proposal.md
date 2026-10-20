# Change: 019 — graph index bootstrap

## Why
`_ensure_graph_schema` (pipeline.py) auto-creates only two
`SourceItem` property indexes. Since 018 (REL edge materialization) the
graph also carries `:Entity` nodes and `:REL` edges, but no index on the
properties the hot paths look them up by:

- `graph_query` entity lookup → `MATCH (e:Entity {name, type})`
- `supersede()` / `drop()` / reconcile → `WHERE r.source_item = $item_id`

At current scale (tens of entities) these are full label scans; as the
graph grows every entity navigation + reconcile pays O(all entities).
The index is schema (DDL), so per the self-contained-shipment model it
must live in code, not config.

## What changes
- `digital_twins/ingest/pipeline.py`: `_ensure_graph_schema()` gains two
  idempotent statements, appended after the existing `SourceItem` ones:
  ```
  CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name, e.type)
  CREATE INDEX IF NOT EXISTS FOR (r:REL) ON (r.source_item)
  ```
  Same session-API choke point (`_execute_cypher`), same auto-apply on
  every graph-enabled pass — no operator step, no migration.

## Impact
- `digital_twins/ingest/pipeline.py` (2 statements + docstring)
- `tests/unit/test_pipeline.py` (extend: assert the 2 new indexes exist)
- `specs/013-neo4j-query-tests` test-contract delta (index assertions)
- Semver: PATCH (additive DDL; no behaviour change, no new knob)

## Non-goals
No `:MENTIONED` relationship index (follow-up); no query-planner /
EXPLAIN tooling; no change to `graph_query`, `supersede`, or `drop`
logic; no new config knob; no data migration.
