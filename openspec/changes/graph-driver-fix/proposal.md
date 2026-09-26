# Change: Fix the Neo4j graph-write driver contract (BUG-01)

## Why
UAT found `POST /api/ingest/run` (and CLI `digital-twins run`) crash with
`AttributeError: 'BoltDriver' object has no attribute 'run'`. The pipeline's
S4 graph write helpers (`_upsert_graph`, `_ensure_graph_schema` in
`digital_twins/ingest/pipeline.py`) call `neo4j.run(...)`, but
`scheduler/loop.py::build_neo4j_driver` returns a raw `neo4j.GraphDatabase.driver`
BoltDriver, which has no `.run()` method. Unit tests fake a driver-shaped `.run()`
so the test suite stays green — the bug slipped through.

This change fixes the production driver contract so ingestion can complete when
Neo4j is configured.

## What changes
- Update the S4 graph-write helpers to use the real `neo4j` driver session API
  (`driver.session()`, `session.run()`) rather than the test-only `.run()`
  surface.
- Add an integration test that exercises a real driver-shaped fake with
  `.session()`/`.run()` semantics to catch this contract mismatch.

## Non-goals
- No change to the graph schema (`SourceItem`, `Entity`, `MENTIONED`, `REL`).
- No change to Qdrant payload shape or point-ID algorithm.
- No change to preflight hard/optional service dependencies (separate change).
- No new source adapters.

## Impact
- Affected code: `digital_twins/ingest/pipeline.py`,
  `digital_twins/scheduler/loop.py`,
  `tests/integration/test_neo4j_query.py`.
- Unblocks end-to-end NFR-14 idempotent scheduling / NFR-1 dedup UAT.
- No new dependencies.
