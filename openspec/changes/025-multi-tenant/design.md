# 025 Multi-Tenant Design — two-layer change

The change splits into a **resolution layer** (new, additive) and a
**state layer** (additive migration v6). No existing tool signature
changes; the new behaviour lives below the tool bodies, so 003/004
MCP surface stays backward-compatible for the install tenant.

## Layer 1: store resolution (new module `tenant_service.py`)

- `resolve_stores(conn, tenant_id)` — one SELECT per request (no
  in-module cache; a cache would have to invalidate on
  suspend/delete within the same process, which the spec defers).
  Lookup `tenants` row → `(qdrant_collection, neo4j_database)`.
  Unknown id → `UnknownTenant` (MCP maps to 401); suspended →
  `SuspendedTenant` (403); deleted → `UnknownTenant`. The install
  tenant (id 1) carries the empty-string legacy shared store pair
  `("", "")` — the T2-backfill bridge, **not** `kb-install`/`install`;
  that empty pair is recorded in `apply_v6`'s seeded row.
- `provision_tenant(conn, slug, name, dimension=384)` — create the
  `tenants` row with `qdrant_collection = "kb-" + slug` and
  `neo4j_database = slug` (T1 hard boundary). Slug validation
  (`^[a-z0-9-]+$`, reserved slug `install`) happens **before** the
  INSERT so callers get a clean `InvalidSlug` rather than a raw
  `sqlite3.IntegrityError` from the DDL CHECK. Returns the new
  `tenant_id`.
- `suspend_tenant` / `delete_tenant` — SQLite row-state transitions
  (suspended is reversible; deleted is not, stamps `deleted_at`,
  raises `InstallTenantProtected` for id 1). The row is retained for
  the audit trail (NFR-15 additive-only).
- **Store-side effects are deferred to the provision CLI slice**
  (T021/T022): `provision_tenant` does **not** create the Qdrant
  collection or the Neo4j DB. That is the open work for T008
  (end-to-end `resolve_stores` + `provision_tenant` green gate,
  store-side) — the row-level layer landed, the store-side
  lifecycle is the provision CLI's job.

## Layer 2: state schema (additive migration v6)

`apply_v6` in `state/migrations.py` (`user_version` 5→6,
transactional, NFR-15 additive-only):

- `CREATE TABLE IF NOT EXISTS tenants(...)` — `id`, `slug`
  (UNIQUE, `[a-z0-9-]`), `name`, `status` CHECK
  (`active`/`suspended`/`deleted`), `qdrant_collection`,
  `neo4j_database`, `created_at`, `deleted_at` DEFAULT "".
- `ALTER TABLE accounts ADD COLUMN tenant_id INTEGER NOT NULL
  DEFAULT 1 REFERENCES tenants(id)` — additive column, no rewrite.
- **PK extension on `highwater`** to `(tenant_id, source, item_key)`
  via in-place table swap in one transaction: add `tenant_id NOT
  NULL DEFAULT 1` to the existing table, create `highwater_v6`
  with the extended PK, `INSERT ... SELECT 1, ...` to copy rows,
  `DROP` old, `RENAME` new.
- `ALTER TABLE audit_runs / audit_items ADD COLUMN tenant_id`
  (NOT NULL DEFAULT 1).
- **PK extension on `ingest_locks`** to `(tenant_id, source)` via
  the same in-place swap pattern.
- `CREATE TABLE tenant_service_tokens` (C-1 hash pattern: store the
  hash, return the plaintext exactly once at issue).
- Seed install-tenant row (id=1, slug="install", empty legacy
  shared store pair).

Constitution VI / NFR-15: the whole migration is one transaction;
if it fails, the old tables remain intact. No legacy data rows are
rewritten or dropped.

## Store-side effects (deferred to provision CLI slice)

- **Qdrant**: `CREATE COLLECTION kb-<slug>` at the pinned embedding
  dimension (384-dim bge-small-en-v1.5). `DELETE COLLECTION` on
  tenant delete.
- **Neo4j (Enterprise)**: `CREATE DATABASE <slug>` is async in Neo4j
  5.x — poll to `online` before first write. `DROP DATABASE` on
  delete. Driver URL gets `?db=<slug>` (FR-7 pinning).
- **Neo4j (Community, T5)**: no `CREATE DATABASE`; the tenant's
  channel taxonomy (keyva `keyva*`/`fs` vs all others = kb-pk)
  partitions the default `neo4j` DB. The FR-7 guard verifies the
  pinned query carries the tenant's channel scope; a query without
  channel scope on a tenant install is rejected. Upgrading the box
  to Enterprise activates the `?db=` posture without code changes.

## Auth + tool wiring (MCP/CLI)

- `mcp/auth.py` returns `(tenant_id, account_email)`; the shared
  system token maps to the install tenant (T3).
- Every tool body that resolves a Qdrant collection or Neo4j DB now
  takes `tenant_id` and looks up the store mapping from the
  `tenants` row (`data-model.md`).
- `kb_schedule_*` / `kb_run_history` apply a tenant filter on their
  state-table queries (FR-2); `can_access_schedule`
  (`mcp/acl.py`) gains a same-tenant predicate before the
  owner/admin check.
- New MCP tools: `kb_tenant_list`, `kb_tenant_create`,
  `kb_tenant_suspend`, `kb_tenant_delete`,
  `kb_tenant_service_token` (install-level admin only, T3).
- Error contract: cross-tenant token use → 401 (no fall-through);
  suspended tenant → 403 "tenant suspended".
- CLI: new `digital-twins tenant` subcommand
  (`list` / `create --slug --name` / `suspend --slug` /
  `delete --slug --confirm <slug>` / `service-token create --slug`).
  `account create` gains optional `--slug` (defaults to install
  tenant). Errors surface the `TenantError` message verbatim on
  stderr, exit 2.

## FR-7 Edition-conditional guard

- **Enterprise**: a driver lacking `?db=` on a
  tenant-partitioned write path is fail-fast-rejected (one-line
  validation in `neo4j_migration.py` + doc note in
  `graph_query.py`, tasks T028–T030).
- **Community**: the guard verifies the pinned query carries the
  tenant's channel scope; a query without channel scope on a tenant
  install is rejected. Detect via `?db=` presence in the resolved
  URL or a config knob.
- Standing guards that must stay green: `test_portability.py`
  (T006) + `test_knob_docs.py` (T027) — SC-005/SC-006.

## Semver + docs

- `__version__` → `1.0.0` (MAJOR, A5). CHANGELOG note:
  "store-resolution contract is per-tenant, not install-global".
- `AGENTS.md` SPECKIT marker points at
  `specs/025-multi-tenant/plan.md` (T032).
