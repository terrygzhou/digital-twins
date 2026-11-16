# CLI Contract - 025-multi-tenant

## New subcommands

### tenant group

```
digital-twins tenant list
    Lists tenants (slug, name, status, qdrant_collection, neo4j_database).
    Install-level admin only (T3).

digital-twins tenant create --slug <slug> --name <name>
    Creates a tenant + auto-provisions Qdrant collection + Neo4j
    database at pinned embedding dimension. Prints the slug,
    collection name, and DB name on success.
    Install-level admin only.

digital-twins tenant suspend --slug <slug>
    Sets status=suspended; all that tenants auth tokens now 403.
    Install-level admin only.

digital-twins tenant delete --slug <slug> --confirm <slug>
    Sets status=deleted; drops Qdrant collection + Neo4j database;
    cascades tenant-partitioned state rows. Requires typing the slug
    as confirmation. Recorded in audit trail. Install-level admin only.

digital-twins tenant service-token create --slug <slug>
    Issues a per-tenant service token; returns the plaintext token
    exactly once (stored hashed, C-1 pattern).
```

## Existing subcommand changes

```
digital-twins run --once --as <email>
    Unchanged signature; the resolved tenant now gates the store
    resolution before the pipeline runs (FR-4).

digital-twins account create --email <email> --password <pw> [--slug <slug>]
    003 command gains an optional --slug; new accounts default to the
    install tenant when --slug is omitted.
```
