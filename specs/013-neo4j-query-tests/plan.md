# 013 — neo4j query-path tests (spec-lite, ledger-first slice)

Backfilled 2026-09-08 from `.superpowers/sdd/013-neo4j-query-tests/progress.md`.
Opened 2026-09-01: the package's Neo4j query surfaces had no test coverage —
only reachability was exercised; the actual Cypher was pinned nowhere.

## Scope

NEW `tests/integration/test_neo4j_query.py`:

1. `health.check_neo4j` round-trip: `RETURN 1` connectivity probe plus the
   S4 shape probe (`MATCH (si:SourceItem) RETURN count(si)`), driver closed.
2. `pipeline._upsert_graph` write Cypher pinned (S4 shape, openspec
   s4-graph-alignment): two idempotent `CREATE INDEX IF NOT EXISTS` schema
   bootstrap statements (`SourceItem.item_id`, `SourceItem.channel`), then
   one `MERGE (si:SourceItem {item_id: $id})` per item (deduped on the
   join key) with `channel` + item-level `content_hash` params exact.
   No `KbItem`/`KbChunk`/`HAS_CHUNK` anywhere in the write path — chunk
   text lives in Qdrant `full_content`, not the graph.
3. S4 migration (`neo4j_migration.migrate_s4`): real run issues two
   idempotent `DETACH DELETE` statements (`:KbItem`, `:KbChunk`) and
   reports 0 on a fresh DB; dry-run issues count queries only, no delete.
4. CLI surface `digital-twins migrate s4 [--dry-run]`: driver resolved via
   the config layer (`_cli_resolve_neo4j_driver`), handed to `migrate_s4`;
   unconfigured `neo4j.*` knobs → fail-closed exit 1 with a remediation
   message (no crash).
5. `loop.build_neo4j_driver` wiring: configured url + (user, password).
6. Fail-closed factory: unset knobs → `ConfigError` naming `KB_NEO4J__URL`
   (RED-first — see defect).
7. `@pytest.mark.live` + `DT_NEO4J_LIVE=1`: real read against the
   config-layer-resolved endpoint (NFR-13: no host values in the file).

## Defect found test-first

Case 4 RED: `scheduler/loop.py` imported `ConfigError` from
`digital_twins.config.schema` in both driver factories; it is defined in
`config.loader`, so the fail-closed branch raised `ImportError` instead of
`ConfigError`. 2-line import-path fix.

## S4 delta (2026-09-23)

Opened under openspec `s4-graph-alignment`: the pinned Cypher moved from the
pre-S4 `KbItem`/`KbChunk`/`HAS_CHUNK` shape to the S4 `:SourceItem` shape
(design decision: chunk text stays in Qdrant `full_content`; the graph
holds entity/relation nodes only). Migration cases (3, 4 above) added for
the `digital-twins migrate s4` surface. See the SDD ledger
(`.superpowers/sdd/013-neo4j-query-tests/progress.md`, S4 delta entry) and
`openspec/changes/s4-graph-alignment/tasks.md` §5.

## Status

Complete — commit `00cf529`; full suite 982 passed, 1 skipped; guards green
(see ledger). S4 delta cases passing in `tests/integration/test_neo4j_query.py`
(9 passed, 1 skipped live, sandbox socket-perm pre-existing failures
unrelated).
