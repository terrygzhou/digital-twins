# Plan: 018 — REL edge materialization

## Summary
Fills the one deliberately-left gap from `s4-entity-extraction`:
`materialize()` Step 4. Every other REL piece already exists —
`extract()` parses relations, `supersede()` stamps REL `valid_to`,
`drop()`'s prune checks REL refs. Only the MERGE of `REL` edges is
missing. This makes the graph a true entity graph (entity↔entity),
not just an entity bag.

## Approach
- **Single-file change**: `digital_twings/ingest/entities.py`
  `materialize()`:
  1. Replace the `# Step 4: REL — intentionally no-op` block with a
     loop over `extraction["relations"]`, each MERGE-ing
     `(e1:Entity {subject})-[REL {predicate, source_item,
     prompt_version, valid_from}]->(e2:Entity {object})`;
     ON-MATCH mirrors the MENTIONED step (clear `valid_to`, bump
     `last_seen`/`run_id`).
  2. Accumulate the REL write counter; return
     `{"entities": N, "mentioned": N, "relations": M}` (was constant
     `0`).
- **Gate**: unchanged — REL writes only when `extraction.enabled` and
  a driver exist (the same gate that runs `supersede`/`extract`/
  `materialize`). No new knob.
- **No change** to `extract()`, `supersede()`, `drop()` — already
  REL-aware.

## Files touched
- `digital_twings/ingest/entities.py` (materialize Step 4 + return)
- `tests/unit/test_entities.py` (extend)
- `tests/integration/test_neo4j_query.py` (new REL test, if Neo4j
  available)

## Semver
MINOR. Additive, gated by the existing `extraction.enabled` knob;
default-off → no change.
