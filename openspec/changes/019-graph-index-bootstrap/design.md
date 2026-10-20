# Design: 019 graph index bootstrap

## Context
`_ensure_graph_schema` is the single DDL choke point in the package
(pipeline.py:363): two `CREATE INDEX IF NOT EXISTS` on
`SourceItem.item_id` and `SourceItem.channel`, run from `_upsert_graph`
on every graph-enabled pass. Since `s4-entity-extraction` + 018, the
graph additionally holds `:Entity {name, type, desc, ...}` nodes and
`:REL {predicate, source_item, prompt_version, valid_from, ...}` edges
(`ingest/entities.py::materialize`). Neither is indexed.

Two lookup keys matter:

- **Entity navigation.** `graph_query` / `supersede` resolve an entity
  by `MATCH (e:Entity {name: $name, type: $type})` — exactly the
  composite key `materialize()` MERGEs on. An index key must match the
  lookup key, so the index is composite `(name, type)`, not `(name)`
  alone.
- **Reconcile / reconcile-by-source.** `supersede()`, `drop()`, and any
  per-item reconcile scan `WHERE r.source_item = $item_id` on REL edges
  (the same property the MENTIONED step uses). One index on
  `REL.source_item` serves both.

## Decisions
1. **Extend the existing DDL site; do not add a migration.** Index DDL
   is schema (create-on-write), not a one-shot data migration, so it
   belongs in `_ensure_graph_schema` — idempotent, auto-applied on the
   next graph-enabled run. A separate `.cql` or `migrate` step would
   reintroduce the external-file / remember-to-run problem the
   self-contained model avoids. (Contrast: `neo4j_migration.py::
   migrate_s4` stays a CLI one-shot because legacy data cleanup must
   not re-run every pass.)
2. **Composite key on Entity, scalar key on REL.** `Entity` is
   resolved by `(name, type)` (type-constrained extraction), so index
   the pair. `REL` is resolved by `source_item`, so index that single
   property. Indexing `Entity.name` alone would be a superset that
   never matches the actual MERGE/MATCH predicate.
3. **Idempotent + no-op on re-run.** `CREATE INDEX IF NOT EXISTS`
   returns immediately when the index already exists, so the two new
   statements cost one round-trip per pass on steady-state databases —
   the same guarantee the existing `SourceItem` statements already rely
   on. No version guard needed.
4. **No planner / EXPLAIN tooling in this change.** The win is making
   the lookups cheap; observing it is a separate concern (could be an
   MCP `EXPLAIN` tool, out of scope).

## Non-goals
No `:MENTIONED` relationship index (candidate follow-up:
`CREATE INDEX ... ON (m:MENTIONED)` or on its endpoint); no query
tuning; no change to `graph_query` / `supersede` / `drop` semantics; no
new knob; no data migration; no `:Entity.desc` index (desc is not a
lookup key).
