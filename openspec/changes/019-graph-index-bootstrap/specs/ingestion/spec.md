## Purpose
Behavioural contract for graph index bootstrap in
`pipeline._ensure_graph_schema()`: the S4 schema bootstrap SHALL create
property indexes for the `:Entity` and `:REL` lookups that the hot paths
(`graph_query` entity navigation, `supersede()`/`drop()` reconcile) rely
on, in the same idempotent, auto-applied DDL site as the existing
`SourceItem` indexes.

## ADDED Requirements

### Requirement: graph schema bootstrap indexes Entity and REL
When a graph-enabled pipeline pass runs (`_upsert_graph` →
`_ensure_graph_schema`), the bootstrap SHALL issue, in addition to the
existing `SourceItem.item_id` and `SourceItem.channel` indexes:

```cypher
CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name, e.type)
CREATE INDEX IF NOT EXISTS FOR (r:REL) ON (r.source_item)
```

The `Entity` index SHALL be composite on `(name, type)` to match the
exact key `materialize()` MERGEs and `graph_query`/`supersede` look up
(`MATCH (e:Entity {name, type})`). The `REL` index SHALL be on the
single property `source_item`, which is the key `supersede()`,
`drop()`, and per-item reconcile filter by.

Both statements SHALL be `IF NOT EXISTS` so re-running the bootstrap on
an already-indexed database is a no-op (idempotent, NFR-1), issued
through the single session-API choke point `_execute_cypher`.

#### Scenario: Fresh graph database gets all four indexes
- **WHEN** a graph-enabled pass runs against a Neo4j DB with no graph
  indexes yet
- **THEN** `CALL db.indexes()` lists `SourceItem(item_id)`,
  `SourceItem(channel)`, `Entity(name, type)`, and `REL(source_item)`

#### Scenario: Re-run on an indexed database is a no-op
- **WHEN** a graph-enabled pass runs against a DB where all four
  indexes already exist
- **THEN** the two new `CREATE INDEX IF NOT EXISTS` statements complete
  without error and create no duplicate index

### Requirement: index DDL ships in code, not config
The new index statements SHALL live in `digital_twins/ingest/pipeline.py`
(`_ensure_graph_schema`) as code-bundled DDL — not in a shipped `.cql`
file, a data migration, or a config value — so a self-contained package
install auto-applies them on the next graph-enabled run with no
operator step.

#### Scenario: Self-contained install auto-creates the indexes
- **WHEN** the package is installed and a graph-enabled pass runs on a
  fresh database
- **THEN** the `Entity` and `REL` indexes are present in Neo4j without
  any migration command, config change, or external DDL file
