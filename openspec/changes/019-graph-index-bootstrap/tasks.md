# Tasks: 019 — graph index bootstrap
(Mirrors the 019 OpenSpec delta under `specs/ingestion/`.)

## 1. Unit tests (RED-first)
- [x] 1.1 `tests/unit/test_pipeline.py` (extend):
      - `_ensure_graph_schema(driver)` issues 4 `CREATE INDEX IF NOT
        EXISTS` statements: `SourceItem.item_id`, `SourceItem.channel`,
        `Entity(name, type)`, `REL.source_item` (assert the exact Cypher
        + that the composite Entity key is `(e.name, e.type)`).
      - a second call is a no-op in effect (idempotent; NFR-1) — no
        duplicate DDL is issued beyond `IF NOT EXISTS`.
      **Verification:** index-existence test fails before the 2 new
      statements are added.

## 2. Implementation
- [x] 2.1 `digital_twins/ingest/pipeline.py` `_ensure_graph_schema()`:
      append the two statements after the existing `SourceItem` ones:
      ```
      _execute_cypher(neo4j,
          "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name, e.type)")
      _execute_cypher(neo4j,
          "CREATE INDEX IF NOT EXISTS FOR (r:REL) ON (r.source_item)")
      ```
      Update the docstring: the schema bootstrap now covers `:Entity`
      (composite `(name, type)`, matching the `materialize()` MERGE key)
      and `:REL.source_item` (the reconcile / `supersede` / `drop`
      lookup key). **Verification:** §1.1 tests green.

## 3. Integration (if Neo4j available)
- [x] 3.1 `tests/integration/test_neo4j_query.py` (extend): after
      `kb_ingest` with a graph + `extraction.enabled`, assert
      `SHOW INDEXES` (or `CALL db.indexes()`) lists `Entity(name,
      type)` and `REL(source_item)`; entity lookup
      `MATCH (e:Entity {name, type})` plans via the index.

## 4. Spec-013 test-contract delta (explicit task, per config rule)
- [x] 4.1 `specs/013-neo4j-query-tests/plan.md` (delta): record the two
      new pinned index DDL statements (Entity name+type composite;
      REL source_item) added to `_ensure_graph_schema`, and that the
      013 pinned write contract now counts 4 CREATE INDEX IF NOT EXISTS
      statements per graph pass instead of 2.
- [x] 4.2 `tests/integration/test_neo4j_query.py` `test_upsert_graph_
      cypher_write_queries` (existing pinned test): update the index
      count assertion from 2 to 4 (the test currently hard-pins
      `len(index_q) == 2`; it goes RED the moment §2.1 lands, and the
      013 contract doc and this test must be updated in the same commit
      per the spec-013 delta rule).

## 5. Guards
- [x] 5.1 `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → green (no new knob, no host
      paths).
- [x] 5.2 Full suite.

## 6. Semver + release note
- [x] 6.1 Bump `__version__` (PATCH) + CHANGELOG: "graph schema bootstrap
      now indexes Entity(name, type) and REL(source_item) — idempotent
      DDL auto-applied on every graph-enabled pass."

## 7. Bookkeeping
- [x] 7.1 OpenSpec 019 artifacts validated.
- [x] 7.2 SDD ledger `019-graph-index-bootstrap/progress.md`.
