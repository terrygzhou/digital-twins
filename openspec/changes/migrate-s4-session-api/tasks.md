# Tasks: migrate-s4-session-api

Hotfix slice (EYW-317, follow-up to EYW-316 / PR #5 graph-driver-fix):
`digital_twins/neo4j_migration.py::migrate_s4` still executed Cypher via
driver-level `.run()`, but `scheduler.loop.build_neo4j_driver` returns a
raw `neo4j.GraphDatabase.driver` with no driver-level `.run()` — the same
BUG-01 contract that PR #5 fixed in `ingest/pipeline.py` via the
`_execute_cypher` session-API choke point.

## 1. Driver contract
- [x] 1.1 In `digital_twins/neo4j_migration.py`, route every `migrate_s4`
      Cypher statement (count reads + `DETACH DELETE`s) through the
      session API: `with driver.session() as s: s.run(...)`. No
      driver-level `.run()` remains in the module.

## 2. Tests
- [x] 2.1 RED-first: replace the driver-level `_RecordingMigrator` fake
      in `tests/integration/test_neo4j_query.py` with a session-shaped
      `_SessionRecordingMigrator` (mirroring PR #5's
      `_SessionRecordingNeo4j`), retarget the three existing `migrate_s4`
      spec-scenario tests at it, and add the
      `test_migrate_s4_uses_session_api` regression (AttributeError RED
      against the pre-fix code).
- [x] 2.2 Keep the CLI-surface fake session-shaped
      (`test_cli_migrate_s4_invokes_migrate_s4`).

## 3. Standing guards + NFR-13
- [x] 3.1 Confirm `tests/integration/test_portability.py` (T006) and
      `tests/unit/test_knob_docs.py` (T027) remain green.
- [x] 3.2 No host paths, usernames, or install locations in the shipped
      code (NFR-13): the fix and tests use only the driver/session
      surface; no host values introduced.

## Verification
- `python3 -m pytest tests/integration/test_neo4j_query.py -q`
  (regression RED pre-fix, all GREEN post-fix)
- `python3 -m pytest tests/integration/test_portability.py tests/unit/test_knob_docs.py -q`
