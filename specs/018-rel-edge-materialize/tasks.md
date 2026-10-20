# Tasks: 018 — REL edge materialization

(Mirrors `openspec/changes/018-rel-edge-materialize/tasks.md`.)

## 1. Unit tests (RED-first)
- [ ] 1.1 Extend `tests/unit/test_entities.py`:
      - `materialize()` with 2 relations → REL write counter = 2
      - re-run same extraction → counter = 0 (idempotent, NFR-1)
      - `extract()` filters a relation whose subject/object is not an
        extracted entity (guard holds end-to-end)
      - `materialize()` returns `{"entities":N,"mentioned":N,"relations":M}`
      **Verification:** tests fail before Step 4 is implemented.

## 2. Implementation
- [ ] 2.1 `digital_twings/ingest/entities.py` `materialize()` Step 4:
      MERGE `(e1:Entity {subject})-[REL {predicate, source_item=item_id,
      prompt_version, valid_from}]->(e2:Entity {object})` per parsed
      relation; ON-MATCH mirrors MENTIONED. Replace
      `return {"relations": 0}` with the real REL counter.
      **Verification:** §1.1 tests green.

## 3. Integration
- [ ] 3.1 Extend `tests/integration/test_neo4j_query.py` (if Neo4j in
      env): 1 item, 2 entities + 1 relation →
      `MATCH (e1)-[r:REL]->(e2) WHERE r.source_item = $item_id
      RETURN count(r)` = 1; re-extract → still 1.

## 4. Guards
- [ ] 4.1 `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → green (no new knob, no host
      paths).
- [ ] 4.2 Full suite.

## 5. Semver + release note
- [ ] 5.1 Bump `__version__` (MINOR) + CHANGELOG: "materialize() now
      writes REL edges; gated by extraction.enabled."

## 6. Bookkeeping
- [ ] 6.1 OpenSpec 018 validated.
- [ ] 6.2 SDD ledger `018-rel-edge-materialize/progress.md`.
