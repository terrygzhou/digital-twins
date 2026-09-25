# Capability: ingestion — Neo4j graph-write driver contract

## ADDED Requirements

### Requirement: S4 graph writes use the Neo4j driver session API
`digital_twins/ingest/pipeline.py` graph-write helpers (`_upsert_graph`,
`_ensure_graph_schema`) SHALL execute Cypher through the Neo4j Python driver
session API (`driver.session() -> session.run(cypher, **params)`), not
through a test-only `.run()` method on the driver object. The
`neo4j.GraphDatabase.driver` object returned by
`scheduler/loop.py::build_neo4j_driver` SHALL be usable directly by the
graph-write helpers with no adapter layer.

#### Scenario: Ingest run writes graph nodes when Neo4j is configured
- **WHEN** `neo4j.url` / `neo4j.user` / `neo4j.password` are configured and
  the pipeline runs `run_pipeline` with a graph-enabled source
- **THEN** `_ensure_graph_schema` and `_upsert_graph` execute their Cypher
  through a `driver.session()` context and no `AttributeError` is raised.

#### Scenario: Schema bootstrap remains idempotent
- **WHEN** `_ensure_graph_schema` runs on an already-initialized database
- **THEN** the `CREATE INDEX IF NOT EXISTS` statements are no-ops and the
  pipeline proceeds.

## MODIFIED Requirements
(none)

## REMOVED Requirements
(none)
