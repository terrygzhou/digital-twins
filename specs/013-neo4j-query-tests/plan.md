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

## S4 entity delta (2026-09-24, openspec s4-entity-extraction §5)

Entity/MENTIONED Cypher is pinned in `digital_twins/ingest/entities.py`
(`materialize()`, `supersede()`), not in `tests/integration/test_neo4j_query.py`
(direct-driver Cypher assertions would require a live Neo4j test container,
which the unit-layer recording-driver tests in `tests/unit/test_entities.py`
already cover without the live-mark gate).

**Pinned write shape (extraction.enabled=true path, NFR-1 idempotent):**

Step 1 — per-entity MERGE:
```
MERGE (e:Entity {name: $name, type: $type})
ON CREATE SET e.created = $now, e.desc = $desc
ON MATCH  SET e.last_seen = $now,
             e.desc = CASE WHEN $desc <> '' THEN $desc ELSE e.desc END
```

Step 2 — SourceItem MERGE (per extraction run, deduped on item_id):
```
MERGE (si:SourceItem {item_id: $item_id})
SET si.channel = $channel, si.content_hash = $content_hash,
    si.last_run_id = $run_id, si.last_captured_at = $captured_at
```

Step 3 — per-entity MENTIONED edge:
```
MATCH (si:SourceItem {item_id: $item_id})
MATCH (e:Entity {name: $name, type: $type})
MERGE (si)-[m:MENTIONED]->(e)
ON CREATE SET m.captured_at = $captured_at, m.run_id = $run_id,
             m.prompt_version = $pv, m.valid_from = $now
ON MATCH  SET m.valid_to = NULL, m.last_seen = $now, m.run_id = $run_id
```

Step 4 — REL: intentionally a no-op (design decision 1; REL production is a
follow-up change). `materialize()` returns `{"entities": N, "mentioned": N,
"relations": 0}`.

**Supersede** (`supersede(item_id, driver)`): stamps `valid_to` on all live
`MENTIONED` edges (where `valid_to IS NULL`) scoped to the item's
`SourceItem`; returns the edge count. One statement (REL supersede is a
documented no-op in `supersede()` — REL edges are not yet written, so the
REL branch is a 0-row no-op):

```
MATCH (si:SourceItem {item_id: $item_id})-[m:MENTIONED]->()
WHERE m.valid_to IS NULL
SET m.valid_to = $now
```

**Config gate:** the extraction step runs only when
`extraction.enabled=true` AND a Neo4j driver was passed to `run_pipeline`.
Default is `false` — no LLM call, no entity/MENTIONED writes beyond the
`:SourceItem` node from `pipeline._upsert_graph`.
