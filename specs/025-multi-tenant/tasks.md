# 025 — Multi-Tenant Data Isolation — Tasks

> Generated 2026-11-17 from `plan.md` (Task List, T001–T035). Source of truth for task wording: `plan.md`.

**Checkbox state rationale (2026-11-17, commit `020216b`):** T001 (spec-reading) is done. T003–T005 (apply_v6 migration) and T006–T007 (tenant_service row-level layer + 15 unit tests) landed in commit `020216b` — `apply_v6` is additive/transactional, the install tenant is seeded as id=1 with the empty-string legacy shared store bridge, and the full unit suite passed (1132 tests). As-built: store-side effects (Qdrant collection create/drop, Neo4j DB create/drop) are **deferred to the provision CLI slice**, which is still open — so T008 (end-to-end `resolve_stores` + `provision_tenant` green gate, store-side) and the provision CLI slice (T021/T022) remain unchecked, as does the FR-7 guard (T028–T030), version bump (T031), docs (T032/T035), and standing guards (T033/T034).

## Task List

### Phase 1 — Setup

- [x] **T001**: Read `spec.md`, `data-model.md`, `contracts/mcp.md`, `contracts/cli.md`, `constitution.md`. Confirm live environment state (Qdrant 6333/6335 collections, Neo4j 7687 Community 5.26, `SHOW DATABASES`).
- [x] **T002**: Confirm standing guards green before starting: `pytest tests/integration/test_portability.py` and `pytest tests/unit/test_knob_docs.py`. Record baseline.
  - Baseline 2026-11-17 on `020216b`: `test_portability.py` 5 passed; `test_knob_docs.py` 20 passed. Recorded in the SDD ledger.

### Phase 2 — Foundational (blocking prerequisites)

- [x] **T003 (test-first)**: Write `tests/unit/test_tenant_schema.py` — assert `apply_v6()` creates `tenants` table with all columns, `accounts` has `tenant_id` DEFAULT 1, `highwater`/`ingest_locks` PK includes `tenant_id`, `tenant_service_tokens` exists, `audit_runs`/`audit_items` have `tenant_id`. RED: `apply_v6` does not exist.
  - RED: `apply_v6` does not exist; tests written before implementation (per test-first constitution III).
  - As-built: landed in commit `020216b` as `tests/unit/test_025_migration.py` (16 tests covering `apply_v6` / v6 schema).
- [x] **T004**: Implement `digital_twins/state/migrations.py::apply_v6()` — additive-only DDL: `CREATE TABLE IF NOT EXISTS tenants(...)`, `ALTER TABLE accounts ADD COLUMN tenant_id INTEGER DEFAULT 1 NOT NULL`, `CREATE TABLE highwater_v6 / ingest_locks_v6` with extended PK, copy rows, `DROP` old, `RENAME`; `CREATE TABLE tenant_service_tokens`; `ALTER TABLE audit_runs/items ADD COLUMN tenant_id`. Transactional. Seed install-tenant row (id=1, slug from config or default `"install"`).
  - Additive-only, transactional; in-place table swap for `highwater`/`ingest_locks` PK extension in one txn (NFR-15).
  - As-built (commit `020216b`): `apply_v6` in `state/migrations.py` + `state/models.py`; `connect(state_dir)` auto-migrates on open (WAL, `foreign_keys=ON`).
  - Install-tenant seed: id=1, empty-string legacy shared store pair (`("", "")`) — the T2-backfill bridge, not `kb-install`/`install`.
- [x] **T005**: Run `apply_v6` on a seeded 003-era fixture DB; verify all rows preserved, PKs extended, install tenant row present. GREEN.
  - As-built (commit `020216b`): `tests/unit/test_025_migration.py` verifies rows preserved, PKs extended, install tenant row present.
- [x] **T006 (test-first)**: Write `tests/unit/test_tenant_service.py::test_resolve_stores` — assert install tenant resolves to the legacy shared store pair (`("", "")` — empty strings, the legacy shared collection + default DB bridge, **not** `kb-install`/`install`; the install row's empty pair is the T2-backfill bridge, recorded in `apply_v6`'s seeded row and the decision log); provisioned tenants resolve to `kb-<slug>` / `<slug>`; unknown raises `UnknownTenant`, suspended raises `SuspendedTenant`, deleted raises `UnknownTenant`. RED: `tenant_service` module does not exist. **Done 2026-11-16** (15 tests, RED-then-GREEN).
  - RED: `tenant_service` module did not exist; tests written before implementation.
  - As-built (commit `020216b`): `tests/unit/test_tenant_service.py` (15 tests), RED-then-GREEN.
- [x] **T007**: Implement `digital_twins/tenant_service.py` — **row-level layer only** (SQLite `tenants` table; no Qdrant/Neo4j I/O): `resolve_stores(conn, tenant_id)` (one SELECT, no in-module cache — a cache would need suspend/delete invalidation within the process, deferred to a later pass; the spec's "one lookup cached per request" is satisfied by doing exactly one lookup per request), `provision_tenant(conn, slug, name=None, dimension=384)` (INSERT with slug validation **before** the INSERT so callers get `InvalidSlug`/`ReservedSlug`/`DuplicateSlug` instead of a raw `IntegrityError`; store names derived as `kb-<slug>` / `<slug>`), `suspend_tenant(conn, tenant_id)` (idempotent; install tenant id 1 not suspendable → `UnknownTenant`), `delete_tenant(conn, tenant_id)` (status → deleted + `deleted_at` stamp, row retained for audit, NFR-15; install tenant → `InstallTenantProtected`). **Store-side effects deferred to the provision CLI**: Qdrant `CREATE/DELETE COLLECTION`, Neo4j `CREATE/DROP DATABASE` (Enterprise) — Community 5.26 has no multi-DB, channel taxonomy is the graph boundary (decision log, 2026-11-15), so `provision_tenant` / `delete_tenant` here are row-only and the CLI must create/drop stores around them. **Done 2026-11-16**, unit suite 1132 passed.
  - As-built (commit `020216b`): `tenant_service.py` (313 lines) — `resolve_stores` / `provision_tenant` / `suspend_tenant` / `delete_tenant` with named exceptions (`UnknownTenant` 401, `SuspendedTenant` 403, `InvalidSlug`/`ReservedSlug`/`DuplicateSlug`, `InstallTenantProtected`).
  - Store-side effects deferred to the provision CLI (task-11): Qdrant `CREATE/DELETE COLLECTION`, Neo4j `CREATE/DROP DATABASE` (Enterprise). Community 5.26 has no multi-DB; channel taxonomy is the graph boundary.
- [x] **T008**: Verify `resolve_stores` returns correct `(collection, db_url)` for install tenant and a second tenant; verify `provision_tenant` creates collection at pinned dimension. GREEN.
  - Store-side gate: `provision_tenant` now accepts an optional `qdrant_client` param; when passed, it calls `create_collection(f"kb-{slug}", vector_size=dimension)` before the row is committed (FR-3). Row-only path (no client) remains a no-op on the store side. 5 tests in `tests/unit/test_tenant_service_store_side.py` cover: install-tenant legacy pair, provisioned-tenant kb-pair, pinned-dim collection create, custom-dim collection create, row-only no-client path.

### Phase 3 — User Story 1: Tenant is the isolation unit (P1)

- [ ] **T009 (test-first)**: Write `tests/integration/test_multi_tenant.py::test_cross_tenant_leak` — two tenants ingest same source with same items; tenant-A query path returns zero rows from tenant-B's collection even when owner filter omitted; tenant-A token against tenant-B surface → 401. RED: cross-tenant leak exists today (shared collection).
- [x] **T010**: Modify `digital_twins/mcp/auth.py` to return `(tenant_id, account_email)`; shared system token maps to install tenant. Modify `mcp/registry.py` / `dispatch.py` to thread `tenant_id` through every tool body that resolves a store. Modify `graph_query.py` and `neo4j_migration.py` to build drivers from resolved `KB_NEO4J__URL` with `?db=` (Enterprise) or channel-scope guard (Community).
- [ ] **T011**: Verify SC-001: cross-tenant leak test passes for web, API, MCP, CLI surfaces. GREEN.

### Phase 4 — User Story 2: Tenant-scoped state tables (P1)

- [ ] **T012 (test-first)**: Write `tests/integration/test_multi_tenant.py::test_concurrent_tenant_runs` — two tenants schedule same source/preset; running both concurrently shows two independent `highwater` cursors, two independent `ingest_locks`, two independent `audit` trails; tenant-A's run-history shows zero rows from tenant-B. RED: today `highwater`/`ingest_locks` are install-global (single PK).
- [ ] **T013**: Modify `digital_twins/state/db.py` query helpers to include `tenant_id` in all `highwater`/`ingest_locks`/`audit_*` reads/writes. Modify `mcp/acl.py::can_access_schedule` to add same-tenant predicate before owner/admin check.
- [ ] **T014**: Verify SC-002: two tenants, same source, concurrent runs — independent state rows, zero cross-tenant lock contention. GREEN.

### Phase 5 — User Story 3: Tenant lifecycle (P2)

- [ ] **T015 (test-first)**: Write `tests/unit/test_tenant_service.py::test_tenant_lifecycle` — `provision_tenant("kb-pk", 384)` creates collection + DB (Enterprise) / channel partition (Community); `suspend_tenant` makes all auth refuse service with 403 "tenant suspended"; `delete_tenant` drops collection + DB + state rows, audit trail records the event. RED: lifecycle functions do not exist.
- [ ] **T016**: Implement `tenant_service.py` lifecycle: `provision_tenant` (Enterprise: `CREATE DATABASE` + poll `SHOW DATABASES`; Community: channel taxonomy), `suspend_tenant` (status → suspended, all tokens reject), `delete_tenant` (confirmation + audit; drop collection, Enterprise: `DROP DATABASE`; Community: no-op for graph; delete state rows; status → deleted).
- [ ] **T017**: Verify SC-004: create→first-run green; suspend→403 "suspended"; delete→stores + state rows gone. GREEN.

### Phase 6 — User Story 4: Tenant-scoped service tokens (P2)

- [x] **T018 (test-first)**: Write `tests/unit/test_tenant_service_tokens.py` — per-tenant service token authenticates only that tenant's runs; presenting tenant-A token to tenant-B surface → 401; revoking A's token does not affect B. Legacy shared `system` token maps to install tenant. RED: token-to-tenant resolution does not exist.
- [x] **T019**: Implement `tenant_service_tokens` CRUD in `tenant_service.py`; modify `mcp/auth.py` to resolve token → `(tenant_id, account_email)`; cross-tenant = 401, fail-closed.
- [x] **T020**: Verify SC-001 (token dimension): tenant-A service token authenticates only A; B's surface rejects A's token with 401. GREEN.

### Phase 7 — CLI + Web surfaces (P2)

- [ ] **T021 (test-first)**: Write `tests/unit/test_cli_tenant.py` — `digital-twins tenant list` returns tenants; `tenant create kb-pk` provisions; `tenant suspend kb-pk` suspends; `tenant delete kb-pk --confirm` deletes; `tenant service-token kb-pk` issues/revokes. RED: CLI subcommand does not exist.
- [ ] **T022**: Implement `digital_twins/cli.py::tenant` subcommand group (list, create, suspend, delete, service-token).
  - Includes the deferred store-side effects: Qdrant `CREATE/DELETE COLLECTION`, Neo4j `CREATE/DROP DATABASE` (Enterprise) around the row-only `provision_tenant` / `delete_tenant`.
- [ ] **T023 (test-first)**: Write `tests/unit/test_web_tenant.py` — web admin panel shows tenant list (install admin only); tenant-scoped run history; cross-tenant = 401, suspended = 403.
- [ ] **T024**: Implement `digital_twins/web/app.py` tenant-aware endpoints.

### Phase 8 — Backfill + Migration (T2)

- [ ] **T025 (test-first)**: Write `tests/integration/test_backfill.py` — seeded 003-era DB with legacy shared collection points; run `apply_v6` + backfill; verify point-count invariant per `owner_tag` bucket; Neo4j legacy DB node/edge counts match install-tenant DB after copy (Enterprise) / no-op (Community). RED: backfill script does not exist.
- [ ] **T026**: Implement `digital_twins/backfill.py` (NEW) — `backfill_install_tenant(qdrant_source_collection, qdrant_target_collection)` via `qdrant_client.upload_points` (payload-only, no re-embedding, `item_key`-indexed); Neo4j cross-DB copy node-id-preserving (Enterprise only; Community: no-op). One-shot migration script.
- [ ] **T027**: Verify SC-003: migration on seeded legacy install passes with no data loss; point-count invariant holds; Neo4j counts match. GREEN.

### Phase 9 — FR-7 Edition-conditional guard

- [ ] **T028 (test-first)**: Write `tests/unit/test_fr7_guard.py` — on Enterprise: driver without `?db=` on tenant-partitioned write path → fail-fast error. On Community: query without channel scope on tenant install → rejected. Both paths covered.
- [ ] **T029**: Implement FR-7 guard in `neo4j_migration.py` + `graph_query.py`: Edition-conditional (detect via `?db=` presence in resolved URL or config knob). Enterprise: `?db=` required. Community: channel-scope check. One-line validation + doc note.
- [ ] **T030**: Verify FR-7: guard rejects missing `?db=` on Enterprise; guard rejects missing channel scope on Community. GREEN.

### Phase 10 — Version bump + Docs + Standing guards

- [ ] **T031**: Bump `digital_twins/__init__.py` to `1.0.0` (MAJOR, A5). Add migration note to release docs.
- [ ] **T032**: Update `AGENTS.md` SPECKIT marker to point at `specs/025-multi-tenant/plan.md`.
- [ ] **T033**: Run standing guards: `pytest tests/integration/test_portability.py` (T006) + `pytest tests/unit/test_knob_docs.py` (T027) — both must stay green (SC-005/SC-006).
- [ ] **T034**: Full test suite: `pytest tests/` — all green.
- [ ] **T035**: Update `contracts/mcp.md` and `contracts/cli.md` to reflect final implementation (if any deviations from the contract arose during implementation).
