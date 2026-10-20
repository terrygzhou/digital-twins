# Tasks: 018 — REL edge materialization
(Mirrors `specs/018-rel-edge-materialize/`.)

## 1. Unit tests (RED-first)
- [x] 1.1 `tests/unit/test_entities.py` (extend):
      - `materialize()` with 2 relations → REL write counter = 2
      - re-run same extraction → counter = 0 (idempotent, NFR-1)
      - `extract()` filters a relation whose subject/object is not an
        extracted entity (assert the guard holds end-to-end)
      - `materialize()` returns `{"entities":N,"mentioned":N,"relations":M}`
      **Verification:** tests fail before Step 4 is implemented.

## 2. Implementation
- [x] 2.1 `digital_twings/ingest/entities.py` `materialize()` Step 4:
      for each relation in `extraction["relations"]`, MERGE
      `(e1:Entity {subject})-[REL {predicate, source_item=item_id,
      prompt_version, valid_from}]->(e2:Entity {object})`;
      ON-MATCH clears `valid_to`, bumps `last_seen`/`run_id` (mirror the
      MENTIONED step). Replace the `return {"relations": 0}` with the
      actual REL write counter. **Verification:** §1.1 tests green.

## 3. Integration
- [x] 3.1 `tests/integration/test_neo4j_query.py` (extend, if Neo4j
      available): one item, 2 entities + 1 relation →
      `MATCH (e1:Entity)-[r:REL]->(e2:Entity) WHERE r.source_item
      = $item_id RETURN count(r)` = 1; re-extract → still 1 (no dupes).

## 4. Guards
- [x] 4.1 `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → green (no new knob, no host
      paths).
- [x] 4.2 Full suite.

## 5. Semver + release note
- [x] 5.1 Bump `__version__` (MINOR) + CHANGELOG: "materialize() now
      writes REL edges (entity↔entity); gated by extraction.enabled."

## 6. Bookkeeping
- [x] 6.1 OpenSpec 018 artifacts validated.
- [x] 6.2 SDD ledger `018-rel-edge-materialize/progress.md`.
