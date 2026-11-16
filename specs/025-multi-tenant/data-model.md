# Data Model: Multi-Tenant Data Isolation

All tables live in the 001 state store (`state.db`). 025 adds
**one new table** (`tenants`), extends **one existing table**
(`accounts` gains `tenant_id`), and retires the install-global
primary keys on `highwater` / `ingest_locks` by adding
`tenant_id` to their primary keys.

Constitution VI / NFR-15: the migration is **additive-only**
(`apply_v6`, transactional, `user_version` 5→6). Existing rows gain
a `tenant_id` default (the install tenant) so no data loss and no
rewrite. The primary-key change on `highwater` / `ingest_locks` is
implemented by:
(a) adding `tenant_id` NOT NULL DEFAULT <install-tenant> to the
existing tables,
(b) creating a new table with the extended primary key,
(c) copying rows,
(d) dropping the old table,
(e) renaming — all inside one transaction. If the transaction
fails, the old tables remain intact.

## Migration v6 (additive)

`migrations.MIGRATIONS` gains `(6, apply_v6)`;
`SCHEMA_VERSION` becomes 6.

DDL:

```sql
-- 1. tenants (new).
CREATE TABLE IF NOT EXISTS tenants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,           -- deterministic, [a-z0-9-], immutable in v1
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT "active"
                CHECK (status IN ("active", "suspended", "deleted")),
    qdrant_collection TEXT NOT NULL,            -- derived from slug at create: "kb-" + slug
    neo4j_database    TEXT NOT NULL,            -- derived from slug at create: slug itself;
                                                -- on Neo4j Community edition the driver
                                                -- ignores this and partitions by channel
                                                -- scope within the default DB instead (T5);
                                                -- the value stays stored for the
                                                -- Enterprise `?db=` upgrade path.
    created_at  TEXT NOT NULL,
    deleted_at  TEXT NOT NULL DEFAULT ""
);
-- The install tenant is seeded by the migration itself:
--   slug="install", status=active, qdrant/neo4j = current
--   collection/DB from config.

-- 2. accounts: additive tenant_id column.
ALTER TABLE accounts ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1
    REFERENCES tenants(id);

-- 3. highwater: extend primary key (tenant-partitioned).
CREATE TABLE highwater_v6 (
    tenant_id  INTEGER NOT NULL,
    source     TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    last_key   TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source, item_key),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
INSERT INTO highwater_v6 (tenant_id, source, item_key, last_key, updated_at)
    SELECT 1, source, item_key, last_key, updated_at FROM highwater;
DROP TABLE highwater;
ALTER TABLE highwater_v6 RENAME TO highwater;

-- 4. audit_runs / audit_items: additive tenant_id.
ALTER TABLE audit_runs  ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1
    REFERENCES tenants(id);
ALTER TABLE audit_items  ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1
    REFERENCES tenants(id);

-- 5. ingest_locks: extend primary key (tenant-partitioned).
CREATE TABLE ingest_locks_v6 (
    tenant_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
INSERT INTO ingest_locks_v6 (tenant_id, source, run_id, acquired_at)
    SELECT 1, source, run_id, acquired_at FROM ingest_locks;
DROP TABLE ingest_locks;
ALTER TABLE ingest_locks_v6 RENAME TO ingest_locks;

-- 6. tenant_service_tokens (per-tenant analogue of the shared system token).
CREATE TABLE IF NOT EXISTS tenant_service_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    INTEGER NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

-- 7. tenant_config: NOT created in v1 (T4 = no per-tenant endpoint
-- overrides; spec §Decisions). Added as a follow-up feature if the
-- C-3 precedence chain is ever extended to a per-tenant level.
```

## Qdrant / Neo4j store mapping

Deterministic from `tenants.slug`:

| Field | Derivation |
|---|---|
| `qdrant_collection` | `"kb-" + slug` (e.g. `kb-acme`) |
| `neo4j_database` | `slug` (e.g. `acme`) — the Neo4j 5.x database name |

**Collision contract (A6):** the `kb-` prefix makes Qdrant
collection names disjoint from the pinned `personal_kb`
(single-tenant default, `health.py` L18) and `mem0` (external).
`personal_kb` and `mem0` are **reserved** and cannot be produced by
slug expansion — slug is `[a-z0-9-]` and the prefix is fixed. The
Neo4j DB name is the bare slug; the `neo4j` system database and any
operator-chosen default DB name are outside the slug expansion
space by construction (slugs are tenant-provisioned, never `neo4j`
or a system DB — the provisioner rejects slug values that collide
with `neo4j` or any operator-pinned system DB name, NFR-13
companion).

The mapping is stored on the `tenants` row at creation time so a
rename requires an explicit store-migration step (follow-up); in
v1 the slug is treated as immutable after creation.

## Store resolution at query time

`tenant_id` → `tenants.row` → `(qdrant_collection,
neo4j_database)` — one lookup, cached for the request duration.
No per-row store lookup; the collection/DB is resolved once per
authenticated caller. The Neo4j driver is built from the resolved
`KB_NEO4J__URL` **including** the `?db=` component (FR-7 guard);
a driver built without `?db=` targets the server default
(`neo4j`) and is fail-fast-rejected on any tenant-partitioned
write path.

## Provisioning (tenant create, FR-3)

1. `CREATE COLLECTION kb-<slug>` in Qdrant at the pinned
   embedding dimension (007 S2-mismatch guard: the pinned
   dimension must match the model's; the provisioner rejects a
   mismatch before first write).
2. `CREATE DATABASE <slug>` in Neo4j 5.x; poll `SHOW DATABASES`
   until the DB reports `online` (creation is asynchronous; the
   DB reaches `online` after an undefined interval).
3. The `tenants` row is committed only after both stores are
   ready (transactional from the state-DB side; the Neo4j/Qdrant
   side is verified by the poll loop). A fresh tenant's first
   run succeeds end-to-end (NFR-12, SC-004).

## Deletion (tenant delete, FR-3)

- `DROP COLLECTION kb-<slug>` in Qdrant.
- `DROP DATABASE <slug>` in Neo4j (the provisioner checks the DB
  is `online` before dropping; a suspended-then-deleted tenant's
  DB is dropped in its current state).
- `DELETE FROM tenants WHERE id = <tenant_id>` cascades
  (state-DB side, via the FKs declared above) —
  `highwater`, `ingest_locks`, `tenant_service_tokens` rows
  vanish; `accounts` rows are NOT cascaded (an account without a
  tenant is invalid, so the delete is rejected if accounts
  still reference the tenant; the operator must reassign or
  delete accounts first).
- The audit trail records the event with the acting admin
  (SC-004).

## Backfill (T2 = copy-in-place)

- **Qdrant:** bulk `qdrant_client.upload_points` from the legacy
  shared collection into `kb-install` (the install tenant's
  collection), preserving point payloads (including `owner_tag`).
  No re-embedding. Skips the 007 S2 dimension-guard pass.
- **Neo4j:** the install tenant's DB is created empty; legacy
  graph nodes/edges are copied cross-DB (node ids preserved) via
  a one-shot migration script. `:SourceItem` ownership follows
  the S4 write path.
- The legacy shared collection and the legacy default-DB graph
  are retired from the write path only after backfill completes
  and a point-count invariant check passes (per-`owner_tag`
  bucket, SC-003; node/edge counts match on the Neo4j side).

## T5 consequence note (Community edition)

On Neo4j Community 5.26 (the live shared box, verified 2026-11-15):

- **No cross-DB Neo4j backfill.** The legacy graph already lives in
  the default `neo4j` DB; channel partitioning (keyva channels
  `keyva*`/`fs` vs all others = kb-pk) provides the hard boundary.
  The cross-DB node-id-preserving copy described above applies
  only to Enterprise installations; on Community the one-shot
  migration script is a no-op for Neo4j (the install tenant's
  graph IS the default-DB graph, scoped by channel at query time).
- **`neo4j_database` stores the slug but is not pinned.** The
  `tenants.neo4j_database` field remains populated with the slug
  (Enterprise-ready), but the Community driver does NOT append
  `?db=<slug>` to `KB_NEO4J__URL`; it targets the default `neo4j`
  DB and the hard boundary is the channel taxonomy. Upgrading to
  Enterprise activates the `?db=` posture without code changes.
- **FR-7 guard is Edition-conditional.** On Enterprise: a driver
  lacking `?db=` is fail-fast-rejected on tenant-partitioned write
  paths. On Community: the guard verifies the pinned query carries
  the tenant's channel scope; a query without channel scope on a
  tenant install is rejected.
- **Enterprise path unchanged.** The cross-DB copy, `CREATE
  DATABASE` polling, and `?db=` pinning remain the documented
  Enterprise path; Community installs simply skip those steps.
