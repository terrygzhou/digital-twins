# Change: 018 — REL edge materialization

## Why
`s4-entity-extraction` parsed LLM relations and stamped `valid_to`
on REL edges in `supersede()`, but `materialize()` Step 4 is a
deliberate no-op (design decision 1). The graph therefore has no
entity↔entity edges, so `kb_search` expansion can only follow
`MENTIONED` (source→entity), not `REL` (entity→entity).

## What changes
- `digital_twings/ingest/entities.py`: `materialize()` Step 4
  MERGEs `(e1)-[REL {predicate, source_item, prompt_version,
  valid_from}]->(e2)` for each parsed relation; ON-MATCH mirrors the
  MENTIONED logic (clear `valid_to`, bump `last_seen`/`run_id`).
  Return dict `"relations"` field changes from constant `0` to the
  actual REL write counter.
- No new knob, no prompt change, no `supersede()`/`drop()` change
  (both already REL-aware).
- **Gate**: REL edges are written only when `extraction.enabled` is
  true AND a Neo4j driver is present — the same gate as entity
  extraction. Default (knob off) → no REL writes, no behaviour
  change.

## Impact
- `ingest/entities.py` (materialize Step 4 + return dict)
- `tests/unit/test_entities.py` (extend)
- `tests/integration/test_neo4j_query.py` (new REL test, if Neo4j
  available)
- Semver: MINOR (additive; gated by existing knob)

## Non-goals
No cross-item REL; no prompt change; no new config.
