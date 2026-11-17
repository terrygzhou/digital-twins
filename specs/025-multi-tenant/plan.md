# 025 — Multi-Tenant Data Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 003's soft isolation (shared Qdrant collection + shared Neo4j graph, per-user `owner_tag` post-filter) with hard per-tenant storage boundaries: a dedicated Qdrant collection (`kb-<slug>`) and a dedicated Neo4j database (`<slug>`) per tenant, threaded through every state table, auth surface, and MCP/CLI tool.

**Architecture:** Two-layer change. (1) **Resolution layer**: a new `tenant_service` module that, given a `tenant_id`, looks up the `tenants` row → `(qdrant_collection, neo4j_database)` → builds a Qdrant client pointing at the collection and a Neo4j driver with `?db=` pinned (Enterprise) or channel-scope-guarded (Community, T5). Every query/mutation path resolves stores *before* any filter runs. (2) **State layer**: additive `apply_v6` migration (`user_version` 5→6) that creates `tenants`, `tenant_service_tokens`, adds `tenant_id` to `accounts`/`audit_runs`/`audit_items`, and extends PKs on `highwater`/`ingest_locks` to `(tenant_id, source, item_key)` / `(tenant_id, source)` — all additive-only, transactional (NFR-15).

**Tech Stack:** Python 3.12, stdlib `sqlite3` (state store), `qdrant_client` (Qdrant SDK), `neo4j` (official Python driver), pytest + `http.client` for round-trips; Node.js for any JS-level tests if applicable.

**Spec:** `specs/025-multi-tenant/spec.md` (authority). Data model: `specs/025-multi-tenant/data-model.md`. MCP contract: `specs/025-multi-tenant/contracts/mcp.md`. CLI contract: `specs/025-multi-tenant/contracts/cli.md`.

## Global Constraints

(verbatim requirements from the 025 spec — every task implicitly includes these)

- **FR-1**: Hard isolation per tenant — dedicated Qdrant collection (`kb-<slug>`) + dedicated Neo4j database (`<slug>`); the query/mutation path resolves the caller's stores *before* any filter; `owner_tag` post-filter becomes a within-tenant user filter, not the isolation boundary.
- **FR-2**: Tenancy threads through `highwater`, `audit_runs`, `audit_items`, `ingest_locks` (new `tenant_id` partition key; see `data-model.md` DDL).
- **FR-3**: `tenants` table + tenant lifecycle (create/suspend/delete) with audit; provisioning auto-creates the collection **and** database at the pinned embedding dimension (DB creation is async in Neo4j 5.x — poll to `online` before first write).
- **FR-4**: Token-to-tenant resolution: every auth surface (web session, personal token, service token, MCP, CLI `--as`) carries a tenant before role checks run; cross-tenant token use is rejected (401, fail-closed, no fall-through).
- **FR-5**: Admin scoping per T3: **install-level admin** role crosses tenants; tenant admin does not. Endpoint config stays install-global (T4 = no per-tenant overrides; `tenant_config` NOT created in v1).
- **FR-6** (NFR-15): In-place migration for existing installs: additive DDL (`apply_v6`, `user_version` 5→6), plus a one-shot **copy-in-place** backfill (T2) moving legacy shared-collection points into the install tenant's store; the legacy shared collection is retired from the write path only after backfill completes and the point-count invariant passes.
- **FR-7** (Edition-conditional): All pinned Cypher MUST run against the session's database only; no `USE`/`dbms` cross-DB clause. **Enterprise**: driver construction MUST pin the database via `KB_NEO4J__URL`'s `?db=`; a driver without `?db=` on a tenant-partitioned write path is fail-fast-rejected. **Community (T5)**: driver targets the default `neo4j` DB; the guard verifies the pinned query carries the tenant's channel scope; a query without channel scope on a tenant install is rejected.
- **T1**: Per-tenant hard boundary: dedicated Neo4j DB (`<slug>`) + dedicated Qdrant collection (`kb-<slug>`) per tenant. Not per-user.
- **T2**: Copy-in-place backfill (payload-preserving). No re-embedding.
- **T3**: Install-level admin role; legacy `admin` migrates; tenant admins cannot cross tenants.
- **T4**: No per-tenant endpoint overrides in v1; `tenant_config` NOT created; C-3 stays 3-tier.
- **T5**: Neo4j Community edition + channel partitioning. `tenants.neo4j_database` stores the slug (Enterprise-ready) but Community driver does not pin `?db=`; hard boundary = channel taxonomy. FR-7 becomes Edition-conditional.
- **SC-001**: Cross-tenant leak test — for every query surface (web, API, MCP, CLI), a tenant-A token returns zero rows from tenant B's store even when the owner filter is omitted/absent.
- **SC-002**: Two tenants, same source, concurrent runs — independent `highwater`/`ingest_locks`/`audit` rows; zero cross-tenant lock contention.
- **SC-003**: Migration on a seeded legacy install (003-era data) — in-place upgrade passes with no data loss (point-count invariant per `owner_tag` bucket).
- **SC-004**: Tenant lifecycle — create→first-run green; suspend→all surfaces 403 "suspended"; delete→collection + DB dropped, state rows gone.
- **SC-005**: 001/002 portability + one-record guards stay green; NFR-1 dedup still holds *within* a tenant.
- **SC-006**: Zero host paths / install locations in new code (NFR-13, T027 knob-docs guard stays green).
- NFR-13: No host path, username, or install location in shipped code.
- NFR-15: Additive-only schema migration; never rewrite or drop legacy data rows.
- NFR-16: MAJOR version bump (A5) — store-resolution contract changes.
- Standing guards must stay green: `tests/integration/test_portability.py` (T006), `tests/unit/test_knob_docs.py` (T027).
- Test-first (constitution III): each task lands the failing tests BEFORE the implementation.
- Constitution VI: In-place upgrade preserves local state; schema migration completes before new code starts.

## Context (zero-knowledge engineer's orientation)

- **003 baseline (what exists today):** The package ships single-tenant. One shared Qdrant collection (`personal_kb` on 6333, or the install's configured collection) and one shared Neo4j graph (default `neo4j` DB on 7687) serve all users. Isolation is soft: a per-user `owner_tag` post-filter on query. The state DB (SQLite) has `accounts`, `highwater`, `ingest_locks`, `audit_runs`/`audit_items` with no tenant partition key. The auth layer (`mcp/auth.py`, `accounts.py`) returns `account_email` only; service token = one shared `system` token.

- **What 025 changes (the target):**
  - **New `tenants` table** (id, slug `[a-z0-9-]` immutable, name, status active/suspended/deleted, `qdrant_collection = "kb-"+slug`, `neo4j_database = slug`, created_at, deleted_at). Seeded by migration: one install-tenant row (id=1, slug=`install` or the configured slug).
  - **`accounts` + `tenant_id`** (FK → tenants, DEFAULT 1). All existing accounts belong to the install tenant.
  - **`highwater` / `ingest_locks` PK extended** to `(tenant_id, source, item_key)` / `(tenant_id, source)` — in-place table swap in one txn (NFR-15 additive: create `_v6`, copy, drop, rename).
  - **`tenant_service_tokens`** — per-tenant analogue of the shared `system` token.
  - **`audit_runs` / `audit_items` + `tenant_id`**.
  - **Resolution layer** (`tenant_service.py`): `resolve_stores(tenant_id) -> (qdrant_collection, neo4j_db_url)`; one lookup cached per request. Qdrant client built with the collection; Neo4j driver built from resolved `KB_NEO4J__URL` with `?db=<slug>` appended (Enterprise) or channel-scope guard (Community, T5).
  - **Auth returns `(tenant_id, account_email)`** instead of just `account_email`. Shared system token maps to the install tenant.
  - **New MCP tools**: `kb_tenant_list`, `kb_tenant_create`, `kb_tenant_suspend`, `kb_tenant_delete`, `kb_tenant_service_token`.
  - **CLI**: `digital-twins tenant list|create|suspend|delete|service-token`.
  - **Version bump**: MAJOR (A5).

- **Live environment (verified 2026-11-15):**
  - Qdrant 6333: `personal_kb` (1,506,108 pts — stale hermes-skill artifact, NOT backfill source), `keyva_kb` (29,991 pts — joins keyva tenant with dedup on `item_key`).
  - Qdrant 6335: `digital-twins-kb` (2,298,000 pts), `personal_kb` (4,501,567 pts — the kb-pk backfill source, `item_key`-indexed, HNSW fully built: `indexed_vectors_count` 4,548,941).
  - Neo4j 7474/7687: shared box `neo4j` (shared, `neo4j/graphrag_terry_2026`), **Community 5.26** — no multi-DB (`SHOW DATABASES` = `neo4j` + `system` only; `CREATE DATABASE` and `?db=` are Enterprise-only). `neo4j-dt` (keyva, `neo4j/dt-neo4j`) 7475/7689.
  - Date fields: Qdrant = ISO-8601 UTC strings; Neo4j temporal type out-of-scope (`DateTime`) only on shared box `:Event.last_seen` + `RELATED_TO`/`AT`/`CONTAINS.last_seen`.

- **T5 consequences (Community edition, live box):**
  1. `tenants.neo4j_database` stores the slug (Enterprise-ready) but the Community driver does NOT pin `?db=`; it targets the default `neo4j` DB. Hard boundary = channel taxonomy (keyva channels `keyva*`/`fs` vs all others = kb-pk).
  2. FR-7/§5.3 fail-fast guard becomes Edition-conditional: Enterprise enforces `?db=`; Community relaxes to channel-scope check.
  3. Neo4j backfill on Community = no cross-DB copy (graph already in default DB; channel partitioning provides the boundary). The one-shot migration script is a no-op for Neo4j on Community.
  4. Phase 1 ops-only `?db=` pinning applies to Enterprise installs only; Community installs use channel partitioning from the start.
  5. Version bump still MAJOR (A5).

- **Backfill source (T2, kb-pk tenant):** Qdrant 6335 `personal_kb` (4,501,567 pts) → `kb-kb-pk` on 6333 via `qdrant_client.upload_points` (payload-only: `chunk` → `full_content`/`text`, add `item_key`; **no re-embedding**). Point IDs byte-identical via `uuid5(NAMESPACE_DNS, "kb:{ch}:{item}:{ci}")`. Neo4j: no data move on Community (graph already co-located in default DB).

- **Key files to modify/create:**
  - `digital_twins/state/db.py` — add `tenants`, `tenant_service_tokens`, `tenant_id` to existing tables, PK extension on `highwater`/`ingest_locks`.
  - `digital_twins/state/migrations.py` — `apply_v6()` (additive-only, transactional).
  - `digital_twins/tenant_service.py` (NEW) — `resolve_stores(tenant_id)`, `provision_tenant(slug)`, `suspend_tenant(id)`, `delete_tenant(id)`.
  - `digital_twins/mcp/auth.py` — return `(tenant_id, account_email)`; map shared system token to install tenant.
  - `digital_twins/mcp/registry.py` / `dispatch.py` — thread `tenant_id` through all tool bodies.
  - `digital_twins/mcp/acl.py` — `can_access_schedule` gains same-tenant predicate.
  - `digital_twins/graph_query.py` — FR-7 guard note; Edition-conditional channel-scope check.
  - `digital_twins/neo4j_migration.py` — FR-7 guard; Edition-conditional.
  - `digital_twins/cli.py` — `digital-twins tenant list|create|suspend|delete|service-token`.
  - `digital_twins/web/app.py` — tenant-aware surfaces; cross-tenant = 401, suspended = 403.
  - `digital_twins/__init__.py` — MAJOR version bump (e.g. `1.0.0`).
  - `tests/unit/test_tenant_service.py` (NEW) — resolution, provisioning, lifecycle.
  - `tests/integration/test_multi_tenant.py` (NEW) — cross-tenant leak (SC-001), concurrent runs (SC-002), migration (SC-003), lifecycle (SC-004).

## Task List

Test-first: each task starts with a failing test, then the implementation that makes it pass.

### Phase 1 — Setup

- [ ] **T001**: Read `spec.md`, `data-model.md`, `contracts/mcp.md`, `contracts/cli.md`, `constitution.md`. Confirm live environment state (Qdrant 6333/6335 collections, Neo4j 7687 Community 5.26, `SHOW DATABASES`).
- [ ] **T002**: Confirm standing guards green before starting: `pytest tests/integration/test_portability.py` and `pytest tests/unit/test_knob_docs.py`. Record baseline.

### Phase 2 — Foundational (blocking prerequisites)

- [ ] **T003 (test-first)**: Write `tests/unit/test_tenant_schema.py` — assert `apply_v6()` creates `tenants` table with all columns, `accounts` has `tenant_id` DEFAULT 1, `highwater`/`ingest_locks` PK includes `tenant_id`, `tenant_service_tokens` exists, `audit_runs`/`audit_items` have `tenant_id`. RED: `apply_v6` does not exist.
- [ ] **T004**: Implement `digital_twins/state/migrations.py::apply_v6()` — additive-only DDL: `CREATE TABLE IF NOT EXISTS tenants(...)`, `ALTER TABLE accounts ADD COLUMN tenant_id INTEGER DEFAULT 1 NOT NULL`, `CREATE TABLE highwater_v6 / ingest_locks_v6` with extended PK, copy rows, `DROP` old, `RENAME`; `CREATE TABLE tenant_service_tokens`; `ALTER TABLE audit_runs/items ADD COLUMN tenant_id`. Transactional. Seed install-tenant row (id=1, slug from config or default `"install"`).
- [ ] **T005**: Run `apply_v6` on a seeded 003-era fixture DB; verify all rows preserved, PKs extended, install tenant row present. GREEN.
- [ ] **T006 (test-first)**: Write `tests/unit/test_tenant_service.py::test_resolve_stores` — assert install tenant resolves to the legacy shared store pair (`("", "")` — empty strings, the legacy shared collection + default DB bridge, **not** `kb-install`/`install`; the install row's empty pair is the T2-backfill bridge, recorded in `apply_v6`'s seeded row and the decision log); provisioned tenants resolve to `kb-<slug>` / `<slug>`; unknown raises `UnknownTenant`, suspended raises `SuspendedTenant`, deleted raises `UnknownTenant`. RED: `tenant_service` module does not exist. **Done 2026-11-16** (15 tests, RED-then-GREEN).
- [x] **T007**: Implement `digital_twins/tenant_service.py` — **row-level layer only** (SQLite `tenants` table; no Qdrant/Neo4j I/O): `resolve_stores(conn, tenant_id)` (one SELECT, no in-module cache — a cache would need suspend/delete invalidation within the process, deferred to a later pass; the spec's "one lookup cached per request" is satisfied by doing exactly one lookup per request), `provision_tenant(conn, slug, name=None, dimension=384)` (INSERT with slug validation **before** the INSERT so callers get `InvalidSlug`/`ReservedSlug`/`DuplicateSlug` instead of a raw `IntegrityError`; store names derived as `kb-<slug>` / `<slug>`), `suspend_tenant(conn, tenant_id)` (idempotent; install tenant id 1 not suspendable → `UnknownTenant`), `delete_tenant(conn, tenant_id)` (status → deleted + `deleted_at` stamp, row retained for audit, NFR-15; install tenant → `InstallTenantProtected`). **Store-side effects deferred to the provision CLI**: Qdrant `CREATE/DELETE COLLECTION`, Neo4j `CREATE/DROP DATABASE` (Enterprise) — Community 5.26 has no multi-DB, channel taxonomy is the graph boundary (decision log, 2026-11-15), so `provision_tenant` / `delete_tenant` here are row-only and the CLI must create/drop stores around them. **Done 2026-11-16**, unit suite 1132 passed.
- [ ] **T008**: Verify `resolve_stores` returns correct `(collection, db_url)` for install tenant and a second tenant; verify `provision_tenant` creates collection at pinned dimension. GREEN.

### Phase 3 — User Story 1: Tenant is the isolation unit (P1)

- [ ] **T009 (test-first)**: Write `tests/integration/test_multi_tenant.py::test_cross_tenant_leak` — two tenants ingest same source with same items; tenant-A query path returns zero rows from tenant-B's collection even when owner filter omitted; tenant-A token against tenant-B surface → 401. RED: cross-tenant leak exists today (shared collection).
- [ ] **T010**: Modify `digital_twins/mcp/auth.py` to return `(tenant_id, account_email)`; shared system token maps to install tenant. Modify `mcp/registry.py` / `dispatch.py` to thread `tenant_id` through every tool body that resolves a store. Modify `graph_query.py` and `neo4j_migration.py` to build drivers from resolved `KB_NEO4J__URL` with `?db=` (Enterprise) or channel-scope guard (Community).
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

## Complexity Tracking

| Deviation | Simpler alternative | Reason |
|---|---|---|
| FR-7 Edition-conditional guard (Enterprise `?db=` vs Community channel-scope) | Single `?db=` guard for all editions | Community 5.26 has no multi-DB; `?db=` is Enterprise-only. Channel partitioning is the A1-hard baseline documented in AGENTS.md; the guard must be Edition-conditional to work on the live Community box without a license upgrade. |
| In-place table swap for `highwater`/`ingest_locks` PK extension | Drop + recreate | NFR-15 additive-only: never rewrite or drop legacy data rows. The swap (create `_v6`, copy, drop, rename in one txn) preserves data. |
| Two-layer change (resolution + state) | Single-layer (state only) | FR-1 requires stores resolved *before* any filter; the resolution layer is the isolation boundary. State layer alone would leave cross-tenant leaks. |

## Open Verification Items (from HANDOFF §4, non-blocking)

- Codex/pi session sources — verify all driver-construction paths (scheduler bootstrap, `run --once`, migration CLI, MCP startup) honour `?db=` (Enterprise) / channel scope (Community).
- Paperclip Postgres parity — verify state tables mirror Postgres schema changes.
- FS source config knobs — verify per-tenant content-pipeline config (tag_map ~40 alias rules) absorbed.
- `?db=` pinning across every driver-construction path — FR-7 guard coverage.

## Live Redeploy Steps

After implementation:

```bash
# 1. Run migration on the live state DB
cd /home/terry/projects/digital-twins-migration/digital-twins-repo
python -m digital_twins.migrate   # apply_v6

# 2. Backfill (kb-pk tenant)
python -m digital_twins.backfill --source personal_kb --target kb-kb-pk --qdrant 6335:6334

# 3. Provision kb-pk tenant
digital-twins tenant create kb-pk --dimension 384

# 4. Verify
digital-twins tenant list
digital-twins tenant service-token kb-pk
```

Standing acceptance: one point per content item across all surfaces (NFR-1/NFR-14).
