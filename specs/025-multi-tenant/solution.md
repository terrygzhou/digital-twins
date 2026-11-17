# Solution: 025 multi-tenant data isolation

## `digital_twins/tenant_service.py` (new module — resolution layer)

Row-level layer only: SQLite `tenants` table I/O, no Qdrant/Neo4j
calls. Landed in commit `020216b` (T006/T007; 15 unit tests in
`tests/unit/test_tenant_service.py`):

- `resolve_stores(conn, tenant_id) -> (qdrant_collection,
  neo4j_database)` — one SELECT per request, no in-module cache
  (suspend/delete would need same-process invalidation; the spec's
  "one lookup cached per request" is satisfied by doing exactly one
  lookup per request). Unknown id → `UnknownTenant` (MCP 401);
  suspended → `SuspendedTenant` (MCP 403 "tenant suspended"); deleted →
  `UnknownTenant`. The install tenant (id 1) carries the
  empty-string legacy shared store pair `("", "")` — the T2-backfill
  bridge, **not** `kb-install`/`install`; that empty pair is recorded
  in `apply_v6`'s seeded row.
- `provision_tenant(conn, slug, name, dimension=384)` — slug
  validation (`^[a-z0-9-]+$`, reserved slug `install`, slug ≠ `neo4j`)
  **before** the INSERT so callers get `InvalidSlug`/`ReservedSlug`/
  `DuplicateSlug` instead of a raw `sqlite3.IntegrityError`; store
  names derived as `kb-<slug>` / `<slug>` (T1 hard boundary).
  Returns the new `tenant_id`.
- `suspend_tenant(conn, tenant_id)` — idempotent status transition;
  install tenant not suspendable. `delete_tenant(conn, tenant_id)` —
  status → `deleted` + `deleted_at` stamp, row retained for audit
  (NFR-15 additive-only); install tenant → `InstallTenantProtected`.
- **Store-side effects are deferred to the provision CLI slice**
  (T021/T022): Qdrant `CREATE/DELETE COLLECTION kb-<slug>`, Neo4j
  `CREATE/DROP DATABASE` (Enterprise; poll `SHOW DATABASES` to
  `online`). Community 5.26 has no multi-DB — the channel taxonomy is
  the graph boundary, so the row-only lifecycle is all this layer
  does.

## `digital_twins/state/migrations.py::apply_v6()` (state layer)

Additive-only, transactional (`user_version` 5→6, Constitution VI /
NFR-15). Landed in commit `020216b` (T003–T005;
`tests/unit/test_025_migration.py`, 16 tests):

- `CREATE TABLE IF NOT EXISTS tenants(...)` — `id`, `slug`
  (UNIQUE, `[a-z0-9-]`), `name`, `status` CHECK
  (`active`/`suspended`/`deleted`), `qdrant_collection`,
  `neo4j_database`, `created_at`, `deleted_at` DEFAULT "".
- `ALTER TABLE accounts ADD COLUMN tenant_id INTEGER NOT NULL
  DEFAULT 1 REFERENCES tenants(id)` — additive, no rewrite.
- **PK extension on `highwater`** to `(tenant_id, source, item_key)`
  via in-place table swap in one transaction: add `tenant_id NOT NULL
  DEFAULT 1`, create `highwater_v6` with the extended PK,
  `INSERT ... SELECT 1, ...` copy rows, `DROP` old, `RENAME` new.
- `ALTER TABLE audit_runs / audit_items ADD COLUMN tenant_id`
  (NOT NULL DEFAULT 1).
- **PK extension on `ingest_locks`** to `(tenant_id, source)` via the
  same swap pattern.
- `CREATE TABLE tenant_service_tokens` (C-1 hash pattern: store the
  hash, return the plaintext exactly once at issue, `revoked` flag,
  cascade on tenant delete).
- Seed install-tenant row: id=1, slug=`install`, `("", "")` legacy
  shared store pair.
- `tenant_config` is **not** created in v1 (T4 — no per-tenant
  endpoint overrides; NFR-13 install-global).

If the transaction fails, the old tables remain intact; no legacy
data rows are rewritten or dropped.

## `digital_twins/mcp/auth.py` + `registry.py` / `dispatch.py` (wiring)

- `mcp/auth.py` returns `(tenant_id, account_email)`; the shared
  system token maps to the install tenant (T3).
- Every tool body that resolves a Qdrant collection or Neo4j DB takes
  `tenant_id` and looks up the store mapping from the `tenants` row.
  The 003/004 MCP surface stays backward-compatible for the install
  tenant (its row resolves to the legacy shared stores).
- `kb_schedule_*` / `kb_run_history` apply a tenant filter on their
  state-table queries (FR-2); `mcp/acl.py::can_access_schedule` gains
  a same-tenant predicate before the owner/admin check.
- New install-admin-only MCP tools: `kb_tenant_list`,
  `kb_tenant_create`, `kb_tenant_suspend`, `kb_tenant_delete`,
  `kb_tenant_service_token` (T3).
- Error contract: cross-tenant token use → 401 (no fall-through);
  suspended tenant → 403 "tenant suspended".

## `digital_twins/cli.py` (tenant subcommand)

New `digital-twins tenant` group (install-level admin only, T3):
`list` / `create --slug --name` (auto-provisions the Qdrant
collection + Neo4j DB at the pinned embedding dimension, 384-dim
bge-small-en-v1.5) / `suspend --slug` / `delete --slug --confirm
<slug>` (cascade store drops + audit log; the slug must be re-entered)
/ `service-token create --slug` (C-1: plaintext token returned
exactly once). `account create` gains an optional `--slug` (defaults
to the install tenant). `run --once --as <email>` signature
unchanged. Errors surface the `TenantError` message verbatim on
stderr, exit 2.

## FR-7 edition-conditional guard

- **Enterprise**: a driver lacking `?db=` on a tenant-partitioned
  write path is fail-fast-rejected — one-line validation in
  `neo4j_migration.py` + doc note in `graph_query.py` (T028–T030).
  Every driver-construction path (scheduler bootstrap, `run --once`,
  migration CLI, MCP startup) must honour `?db=`.
- **Community** (T5, the live shared box, Neo4j 5.26): no
  `CREATE DATABASE`; the tenant's channel taxonomy (keyva
  `keyva*`/`fs` vs all others = kb-pk) partitions the default `neo4j`
  DB. The guard verifies the pinned query carries the tenant's
  channel scope; a query without channel scope on a tenant install is
  rejected. Detected via `?db=` presence in the resolved URL or a
  config knob. Upgrading to Enterprise activates the `?db=` posture
  without code changes.
- Standing guards stay green: `tests/integration/test_portability.py`
  (T006 guard) + `tests/unit/test_knob_docs.py` (T027) — SC-005/
  SC-006.

## `digital_twins/backfill.py` (new module — T2 copy-in-place)

`backfill_install_tenant` copies the legacy shared collection into
the install tenant's target via `qdrant_client.upload_points` —
payload-only (`chunk` → `full_content`/`text`, add `item_key`,
`uuid5(NAMESPACE_DNS, "kb:{ch}:{item}:{ci}")` point IDs preserved),
**no re-embedding**. Point-count invariant check (per-`owner_tag`
bucket, SC-003) gates the legacy write-path retirement. The Neo4j
cross-DB copy is an Enterprise-only step; on Community it is a no-op
(the install tenant's graph **is** the default-DB graph, scoped by
channel at query time).

## `digital_twins/web/app.py`

Tenant-scoped state reads on the token-gated endpoints (same gate as
`kb_search`); cross-tenant and suspended tenants follow the MCP error
contract (401 / 403 "tenant suspended"). No new host paths or config
defaults (NFR-13).

## `tests/` (new suites)

- `tests/unit/test_tenant_service.py` (T006, landed — 15 tests,
  RED-then-GREEN at `020216b`).
- `tests/integration/test_multi_tenant.py` (T009/T012):
  `test_cross_tenant_leak` (two tenants, same source; tenant-A
  query path returns zero rows from tenant-B's collection even
  without the owner filter; tenant-A token against tenant-B surface →
  401) and `test_concurrent_tenant_runs` (two tenants, same
  source/preset; independent `highwater` cursors, `ingest_locks`,
  and audit trails; tenant-A run-history shows zero rows from
  tenant-B).

## `digital_twins/__init__.py` + `CHANGELOG.md` (semver, A5)

`__version__` → `1.0.0` (MAJOR — the store-resolution contract
changes: collection/DB become per-tenant, not install-global).
CHANGELOG note: "store-resolution contract is per-tenant, not
install-global". `AGENTS.md` SPECKIT marker points at
`specs/025-multi-tenant/plan.md` (T032).

## Cross-repo consequence (workspace, not this repo)

The store-resolution contract is the MAJOR boundary for
keyva-ontology's cutover: any keyva code or config that assumes
install-global collection names must re-point at the tenant's
collection + channel scope. The kb-pk cutover gate (backfill point
count + 7-day rollback window) and keyva's A2-box decommission are
planned around it, not against it.
