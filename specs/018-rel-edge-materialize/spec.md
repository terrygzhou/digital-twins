# Feature 018: REL edge materialization (entity ↔ entity)

## Background

`s4-entity-extraction` added LLM extraction of `Entity` + `MENTIONED`
edges. Its `extract()` **already parses `relations`** from the LLM
response, and `supersede()` **already stamps `valid_to` on REL edges** —
but `materialize()` deliberately leaves REL production as a no-op
("design decision 1: REL production is a separate follow-up change").
`entities.py:materialize()` Step 4 says exactly this:
```python
# Step 4: REL — intentionally no-op (design decision 1, follow-up change)
# relations_written = 0
```
This feature implements that follow-up: it turns the already-parsed
relations into real `REL` edges in Neo4j, so the graph is a genuine
entity graph, not an entity bag.

## User Scenarios & Testing

Today, with `extraction.enabled: true`, the graph contains:
```
(Si:SourceItem {item_id})-[MENTIONED]->(e:Entity {name,type})
```
But NO entity↔entity links. So `kb_search`'s graph expansion can find
sibling `SourceItem`s that mention an entity, but it cannot traverse
*which entities are related to which other entities* (e.g.
"KEYVA" —`offers`→ "valve repair", "MOGAS" —`manufactures`→ "ball
valve"). There is no edge to follow.

**After 018**, the graph also contains:
```
(e1:Entity)-[REL {predicate, source_item, prompt_version,
                   valid_from, valid_to}]->(e2:Entity)
```
One `REL` per parsed relation, scoped to the `SourceItem` that produced
it (matching personal-kb's `source_item` scoping — RELs never cross
channels/items).

**Primary scenario**

1. `extraction.enabled: true` and `materialize()` is extended (this
   feature).
2. On each ingest sweep, after `supersede()` + `extract()` (both
   already run), `materialize()` now:
   - MERGEs each `(e1)-[REL {predicate, source_item, prompt_version,
     valid_from}]->(e2)` for every relation in the extraction result
     (subject/object MUST be entities already MERGE'd in Step 1 —
     same constraint `extract()` already enforces).
   - Returns `{"entities": N, "mentioned": N, "relations": M}` with
     `M` the REL write counter.

**Edge cases**

- **Idempotency (NFR-1)**: re-running the same extraction on unchanged
  content MERGEs the same REL edges → `M = 0` on the second run
  (same guarantee `entities` and `mentioned` already have).
- **Supersede parity**: `supersede()` already stamps `valid_to` on the
  item's live REL edges; a re-extraction MERGEs new edges with fresh
  `valid_from` and clears `valid_to` (the ON-MATCH clause mirrors the
  MENTIONED edge logic).
- **Orphan prune on drop()**: `drop()` already DETACH-DELETEs the
  SourceItem; REL edges owned by it are gone with it (DETACH covers
  both ends). The entity prune query in `drop()` already checks
  `NOT (e)-[:REL]->() AND NOT (:Entity)-[:REL]->(e)` — so entities
  referenced ONLY by RELs of the dropped item are NOT pruned if other
  items' RELs still reference them. No change to `drop()` needed.
- **Missing entity on one end** (LLM returned a relation whose
  subject/object was filtered out in Step 1): the relation was
  already filtered by `extract()` (line 208–211: both ends must be in
  `ent_names`). No additional guard needed in `materialize()`.

## Functional Requirements

- **FR-1**: `materialize()` Step 4: for each relation in
  `extraction["relations"]`, MERGE
  `(e1:Entity {name, type})-[REL {predicate, source_item=item_id,
  prompt_version, valid_from}]->(e2:Entity {name, type})`
  with ON MATCH `SET m.valid_to = NULL, m.last_seen = now,
  m.run_id = run_id` (mirror the MENTIONED ON-MATCH logic).
- **FR-2**: `materialize()` return dict gains `"relations": M` where
  M is the non-zero REL write counter (0 on a no-op re-run).
- **FR-3**: REL edges are **only written when `extraction.enabled`
  is true** (same gate as entity extraction) and a Neo4j driver is
  present (same gate). No new knob.
- **FR-4**: Test-First. Unit tests monkeypatch the driver (stub,
  no-op counters) and assert:
  - a fresh extraction with 2 relations → 2 REL write counters
  - a re-run with identical extraction → 0 REL writes (idempotent)
  - a relation whose subject was filtered out in Step 1 does NOT
  appear in `extraction["relations"]` (already enforced by
  `extract()`; test asserts this guard holds end-to-end).
  - `materialize()` returns `{"entities": N, "mentioned": N,
    "relations": M}`.
- **FR-5**: Integration test (real Neo4j, if available in the test
  env): one item with 2 entities + 1 relation → `MATCH (e1)-[r:REL
  {predicate}]->(e2)` returns 1 row; re-extract → still 1 row, no
  duplicates.

## Non-goals

- No cross-item REL (personal-kb's `source_item` scoping is the
  model; RELs never span channels/items).
- No new LLM prompt change: `extract()` already requests and parses
  relations (the prompt is unchanged). This feature only changes what
  gets written to Neo4j.
- No new config knob.
- No change to `supersede()`, `drop()`, `extract()` — they are
  already REL-aware (supersede stamps REL `valid_to`, drop's prune
  query checks REL references).

## Semver

MINOR. New graph behaviour gated by the existing `extraction.enabled`
knob; when that knob is off (default), nothing changes. When on,
REL edges are an additive write to Neo4j — no schema migration
required (the `REL` label is new but Neo4j is schema-less).
