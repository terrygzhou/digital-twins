# 025 Multi-Tenant Data Isolation — OpenSpec tasks

Task list mirroring `specs/025-multi-tenant/plan.md` §Task List,
annotated with implementation state at HEAD (`020216b`).
Test-first per constitution III: each task lands the failing test
before the implementation that makes it pass.

## Phase 1 — Setup

- [x] **T001** — Read `spec.md`, `data-model.md`,
  `contracts/mcp.md`, `contracts/cli.md`, `constitution.md`;
  confirm live environment state (Qdrant 6333/6335 collections,
  Neo4j 7687 Community 5.26, `SHOW DATABASES`).
- [ ] **T002** — Confirm standing guards green before starting:
  `pytest tests/integration/test_portability.py` +
  `pytest tests/unit/test_knob_docs.py`. Record baseline.

## Phase 2 — Foundational (blocking prerequisites)

- [x] **T003** (test-first) — Write
  `tests/unit/test_tenant_schema.py`: assert `apply_v6()` creates
  `tenants` with all columns; `accounts` has `tenant_id` DEFAULT 1;
  `highwater`/`ingest_locks` PK includes `tenant_id`;
  `tenant_service_tokens` exists; `audit_runs`/`audit_items` have
  `tenant_id`. RED: `apply_v6` did not exist.
- [x] **T004** — Implement `digital_twins/state/migrations.py::apply_v6()`:
  additive-only DDL (see design.md §State schema).
- [x] **T005** — Run `apply_v6` on a seeded 003-era fixture DB;
  verify all rows preserved, PKs extended, install tenant row
  present. GREEN.
- [x] **T006** (test-first) — Write
  `tests/unit/test_tenant_service.py::test_resolve_stores`: install
  tenant → `("", "")` legacy shared pair (the T2 bridge, **not**
  `kb-install`/`install`); provisioned tenants → `kb-<slug>`/`<slug>`;
  unknown → `UnknownTenant`; suspended → `SuspendedTenant`; deleted
  → `UnknownTenant`. Done 2026-11-16 (15 tests, RED-then-GREEN).
- [x] **T007** — Implement `digital_twins/tenant_service.py`
  (row-level layer only; store-side effects deferred to provision CLI
  slice — see design.md). Done 2026-11-16, unit suite 1132 passed.
- [ ] **T008** — Verify `resolve_stores` returns correct
  `(collection, db_url)` for install tenant + a second tenant;
  verify `provision_tenant` creates collection at pinned dimension
  (store-side). GREEN.

## Phase 3 — US1: Tenant is the isolation unit (P1)

- [ ] **T009** (test-first) — Write
  `tests/integration/test_multi_tenant.py::test_cross_tenant_leak`:
  two tenants ingest same source with same items; tenant-A query
  path returns zero rows from tenant-B's collection even when owner
  filter omitted; tenant-A token against tenant-B surface → 401.
  RED: cross-tenant leak exists today (shared collection).
- [ ] **T010** — Modify `mcp/auth.py` to return
  `(tenant_id, account_email)`; shared system token maps to install
  tenant. Modify `mcp/registry.py` / `dispatch.py` to thread
  `tenant_id` through every tool body that resolves a store. Modify
  `graph_query.py` / `neo4j_migration.py` to build drivers from
  resolved `KB_NEO4J__URL` with `?db=` (Enterprise) or
  channel-scope guard (Community).
- [ ] **T011** — Verify SC-001: cross-tenant leak test passes for
  web, API, MCP, CLI surfaces. GREEN.

## Phase 4 — US2: Tenant-scoped state tables (P1)

- [ ] **T012** (test-first) — Write
  `tests/integration/test_multi_tenant.py::test_concurrent_tenant_runs`:
  two tenants schedule same source/preset; running both concurrently
  shows two independent `highwater` cursors, two independent
  `ingest_locks`, two independent `audit` trails; tenant-A's
  run-history shows zero rows from tenant-B. RED: today
  `highwater`/`ingest_locks` are install-global (single PK).
- [ ] **T013** — Modify `digital_twins/state/db.py` query helpers to
  include `tenant_id` in all `highwater`/`ingest_locks`/`audit_*`
  reads/writes. Modify `mcp/acl.py::can_access_schedule` to add
  same-tenant predicate before owner/admin check.
- [ ] **T014** — Verify SC-002: two tenants, same source, concurrent
  runs — independent state rows, zero cross-tenant lock contention.
  GREEN.

## Phase 5 — US3: Tenant lifecycle (P2)

- [ ] **T015** (test-first) — Write
  `tests/unit/test_tenant_service.py::test_tenant_lifecycle`:
  `provision_tenant("kb-pk", 384)` creates collection + DB
  (Enterprise) / channel partition (Community); `suspend_tenant`
  makes all auth refuse service with 403 "tenant suspended";
  `delete_tenant` drops collection + DB + state rows, audit trail
  records the event. RED: lifecycle functions do not exist.
- [ ] **T016** — Implement `tenant_service.py` lifecycle store-side
  effects: `provision_tenant` (Enterprise: `CREATE DATABASE` + poll
  `SHOW DATABASES`; Community: channel taxonomy), `suspend_tenant`
  (status → suspended, all tokens reject), `delete_tenant`
  (confirmation + audit; drop collection, Enterprise: `DROP DATABASE`;
  Community: no-op for graph; delete state rows; status → deleted).
- [ ] **T017** — Verify SC-004: create → first-run green; suspend →
  403 "suspended"; delete → stores + state rows gone. GREEN.

## Phase 6 — US4: Tenant-scoped service tokens (P2)

- [x] **T018** (test-first) — Write
  `tests/unit/test_tenant_service_tokens.py`: per-tenant service token
  authenticates only that tenant's runs; presenting tenant-A token to
  tenant-B surface → 401; revoking A's token does not affect B.
  Legacy shared `system` token maps to install tenant. RED:
  token-to-tenant resolution does not exist.
- [x] **T019** — Implement `tenant_service_tokens` CRUD in
  `tenant_service.py`; modify `mcp/auth.py` to resolve token →
  `(tenant_id, account_email)`; cross-tenant = 401, fail-closed.
- [x] **T020** — Verify SC-001 (token dimension): tenant-A service
  token authenticates only A; B's surface rejects A's token with 401.
  GREEN.

## Phase 7 — CLI + Web surfaces (P2)

- [ ] **T021** (test-first) — Write `tests/unit/test_cli_tenant.py`:
  `digital-twins tenant list` returns tenants; `tenant create kb-pk`
  provisions; `tenant suspend kb-pk` suspends; `tenant delete kb-pk
  --confirm` deletes; `tenant service-token kb-pk` issues/revokes.
  RED: CLI subcommand does not exist.
- [ ] **T022** — Implement `digital_twins/cli.py::tenant` subcommand
  group (list, create, suspend, delete, service-token).
- [ ] **T023** (test-first) — Write `tests/unit/test_web_tenant.py`:
  web admin panel shows tenant list (install admin only);
  tenant-scoped run history; cross-tenant = 401, suspended = 403.
- [ ] **T024** — Implement `digital_twins/web/app.py`
  tenant-aware endpoints.

## Phase 8 — Backfill + Migration (T2)

- [ ] **T025** (test-first) — Write
  `tests/integration/test_backfill.py`: seeded 003-era DB with
  legacy shared collection points; run `apply_v6` + backfill;
  verify point-count invariant per `owner_tag` bucket; Neo4j legacy
  DB node/edge counts match install-tenant DB after copy (Enterprise)
  / no-op (Community). RED: backfill script does not exist.
- [ ] **T026** — Implement `digital_twins/backfill.py` (NEW) —
  `backfill_install_tenant(qdrant_source_collection,
  qdrant_target_collection)` via `qdrant_client.upload_points`
  (payload-only, no re-embedding, `item_key`-indexed); Neo4j
  cross-DB copy node-id-preserving (Enterprise only; Community:
  no-op). One-shot migration script.
- [ ] **T027** — Verify SC-003: migration on seeded legacy install
  passes with no data loss; point-count invariant holds; Neo4j
  counts match. GREEN.

## Phase 9 — FR-7 Edition-conditional guard

- [ ] **T028** (test-first) — Write `tests/unit/test_fr7_guard.py`:
  on Enterprise: driver without `?db=` on tenant-partitioned write
  path → fail-fast error. On Community: query without channel scope
  on tenant install → rejected. Both paths covered.
- [ ] **T029** — Implement FR-7 guard in `neo4j_migration.py` +
  `graph_query.py`: Edition-conditional (detect via `?db=` presence
  in resolved URL or config knob). Enterprise: `?db=` required.
  Community: channel-scope check. One-line validation + doc note.
- [ ] **T030** — Verify FR-7: guard rejects missing `?db=` on
  Enterprise; guard rejects missing channel scope on Community.
  GREEN.

## Phase 10 — Version bump + Docs + Standing guards

- [ ] **T031** — Bump `digital_twins/__init__.py` to `1.0.0`
  (MAJOR, A5). Add migration note to release docs.
- [ ] **T032** — Update `AGENTS.md` SPECKIT marker to point at
  `specs/025-multi-tenant/plan.md`.
- [ ] **T033** — Run standing guards:
  `pytest tests/integration/test_portability.py` (T006) +
  `pytest tests/unit/test_knob_docs.py` (T027) — both must stay
  green (SC-005/SC-006).

## Live redeploy (after implementation)

```bash
cd digital-twins-repo
python -m digital_twins.migrate                 # apply_v6
python -m digital_twins.backfill \
  --source personal_kb --target kb-kb-pk \
  --qdrant 6335:6334                            # T2 payload-only
digital-twins tenant create kb-pk --dimension 384
digital-twins tenant list
digital-twins tenant service-token kb-pk
```
