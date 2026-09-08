# 013 — neo4j query-path tests (spec-lite, ledger-first slice)

Backfilled 2026-09-08 from `.superpowers/sdd/013-neo4j-query-tests/progress.md`.
Opened 2026-09-01: the package's Neo4j query surfaces had no test coverage —
only reachability was exercised; the actual Cypher was pinned nowhere.

## Scope

NEW `tests/integration/test_neo4j_query.py`:

1. `health.check_neo4j` round-trip: exactly one read query, `RETURN 1`,
   driver closed.
2. `pipeline._upsert_graph` write Cypher pinned: one `MERGE (n:KbItem …)`
   per distinct item URL + one `MERGE (c:KbChunk …) … HAS_CHUNK` per chunk,
   params exact.
3. `loop.build_neo4j_driver` wiring: configured url + (user, password).
4. Fail-closed factory: unset knobs → `ConfigError` naming `KB_NEO4J__URL`
   (RED-first — see defect).
5. `@pytest.mark.live` + `DT_NEO4J_LIVE=1`: real read against the
   config-layer-resolved endpoint (NFR-13: no host values in the file).

## Defect found test-first

Case 4 RED: `scheduler/loop.py` imported `ConfigError` from
`digital_twins.config.schema` in both driver factories; it is defined in
`config.loader`, so the fail-closed branch raised `ImportError` instead of
`ConfigError`. 2-line import-path fix.

## Status

Complete — commit `00cf529`; full suite 982 passed, 1 skipped; guards green
(see ledger).
