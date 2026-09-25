# Tasks: graph-driver-fix

## 1. Driver contract
- [ ] 1.1 In `digital_twins/ingest/pipeline.py`, replace `neo4j.run(...)` calls
      in `_upsert_graph` and `_ensure_graph_schema` with a
      `with neo4j.session() as s: s.run(...)` helper (or an
      `_execute_cypher(neo4j, cypher, **params)` helper at module level) so
      the graph write works against a real `neo4j.GraphDatabase.driver`.
      Keep `CREATE INDEX IF NOT EXISTS` idempotent (S4 decision unchanged).
- [ ] 1.2 Confirm `digital_twins/scheduler/loop.py::build_neo4j_driver`
      returns the raw driver (no change needed) and add a docstring noting the
      graph-write helpers must use the session API, not the test-only `.run()`.

## 2. Tests
- [ ] 2.1 Extend `tests/integration/test_neo4j_query.py::_RecordingDriver`
      (or add a new fake) to expose `.session()` returning an object with
      `.run()`, and assert `_upsert_graph` / `_ensure_graph_schema` call
      through it. Test-First (RED first against the current production code).
- [ ] 2.2 Add a regression test that the driver is a real `neo4j.driver`
      shape (assert `hasattr(driver, "session")` on the object returned by
      `build_neo4j_driver` when a real driver is constructed — skip when
      `neo4j` is not importable / not configured).

## 3. Standing guards + SDD
- [ ] 3.1 Confirm `tests/integration/test_portability.py` (T006) and
      `tests/unit/test_knob_docs.py` (T027) remain green.
- [ ] 3.2 Run `python3.12 -m pytest tests/integration/test_neo4j_query.py -q`
      and verify the RED-first test in task 2.1 turns green.

## Verification
- `PYTHONPATH=. python3.12 -m pytest tests/integration/test_neo4j_query.py -q`
