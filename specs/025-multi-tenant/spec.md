# Feature Specification: Multi-Tenant Data Isolation

**Feature Branch**: `025-multi-tenant`

**Created**: 2026-11-15 · **Decisions recorded**: 2026-11-15 (A1-hard alignment)

**Status**: Decided (T1–T4 resolved; see §Decisions). `tasks.md` generated at plan stage.

**Input**: Supersedes the deferred hard-isolation decision of Q2
(BR-11.4.3, requirement.md sec.5, DECIDED 2026-08-29: "hard per-user
later"). 003 shipped soft isolation (shared Qdrant collection + shared
Neo4j graph, per-user `owner_tag` post-filter); this feature replaces
post-filter scoping with hard per-tenant storage boundaries.

## Decisions (T1–T4, 2026-11-15)

| Open | Decision | Rationale (A1-hard alignment) |
|---|---|---|
| **T1** | **Per-tenant hard boundary: dedicated Neo4j database + dedicated Qdrant collection per tenant.** Not per-user. | Neo4j 5.x multi-database gives a storage-layer hard boundary for ~zero code (driver URL gets `?db=<tenant_db>`). Qdrant's native boundary is the collection (`kb-<slug>`). One service instance serves all tenants; the trust tier moves with ops, not data model. |
| **T2** | **Copy-in-place backfill** (`qdrant_client.upload_points`, payload-preserving). No re-embedding. | Fast; preserves `owner_tag` so 003 within-tenant user-scoping survives. Neo4j side: tenant DBs are created empty; legacy graph nodes/edges are copied cross-DB (node id preserved) via a one-shot migration script; `:SourceItem` ownership follows the S4 write path. |
| **T3** | **Install-level admin role exists.** The legacy `admin` role migrates to it; a bootstrap/root token maps to it. | Tenant admins cannot cross tenants; install admin can (account mgmt, tenant lifecycle, cross-tenant run history). |
| **T4** | **No per-tenant endpoint overrides in v1.** `tenant_config` table NOT created; LLM/embedding/Qdrant/Neo4j endpoints are install-global (NFR-13). | Keeps C-3 precedence chain at 3 levels; per-tenant overrides are a follow-up feature. |
| **T5** | **Neo4j Edition posture: Community edition + channel partitioning.** The live shared box runs Neo4j 5.26 Community (`neo4j:5` image) — no multi-database support (`CREATE DATABASE` and `?db=` pinning are Enterprise-only). T1's dedicated-DB boundary therefore **degrades to channel-based partitioning** within the default `neo4j` database on Community: `tenants.neo4j_database` stores the slug (Enterprise-ready), but on Community the driver targets the default DB and isolates by channel taxonomy (keyva channels `keyva*`/`fs` vs all others = kb-pk). FR-7's fail-fast guard becomes **Edition-conditional**: on Enterprise, a driver lacking `?db=` is rejected on tenant-partitioned write paths; on Community, the guard verifies channel scope is present in the pinned query. No license cost, no new infrastructure; code remains Enterprise-ready — upgrading the box to Enterprise activates the `?db=` posture without code changes. Per-tenant Neo4j instances (the A2 pattern) remain a documented escape hatch, not the target state. OSS alternative products (FalkorDB/Memgraph) are rejected: they violate the pinned-Cypher session-API contract and the 013 ADR. | Live box verified 2026-11-15: `SHOW DATABASES` on shared `neo4j` = `neo4j` + `system` only (Community 5.26). T1's `?db=` mechanism cannot run as specified without an Enterprise license; channel partitioning is the A1-hard baseline already documented in the workspace AGENTS.md. |

**Superseded assumption**: A4 (Neo4j subgraph as label/naming-namespace
convention, e.g. `Tenant_<slug>_*` label prefix) is **rejected**. The
hard boundary is the Neo4j **database name**, derived from the tenant
slug exactly like the Qdrant collection. A label-prefix boundary is
dead on two counts: (1) the hard boundary costs nothing and is
stronger — every Cypher in the shipped codebase targets the session's
default database, so `?db=` pinning makes isolation automatic; (2) a
label prefix is not "one clobbering write," it is *every* write — the
entire ingest path is unprefixed by construction (`entities.py` MERGE
on `:Entity`, `neo4j_migration.py` legacy-label cleanup), so a soft
boundary requires rewriting every query.

**Superseded assumption**: A5 is resolved — **version bump is MAJOR.**
T1 = per-tenant-hard changes the public store-resolution contract:
after 025, `QDRANT_COLLECTION` / `KB_NEO4J__URL` are per-tenant, not
install-global.

## Two-phase plan (A1-hard now, 025 later)

**Phase 1 — zero code, ops-only (no spec work, no release):**
existing installs that want a hard boundary *today* pin a tenant
database via `KB_NEO4J__URL=neo4j://host:7687?db=<tenant_db>` (Neo4j
5.x multi-DB: `CREATE DATABASE <tenant_db>` first). Qdrant side uses
the existing collection per install. This is A1-hard as an *ops
posture* for trusted tenants sharing one box; it requires no code
change, no knob, no migration. It is NOT a release; it is the
baseline that 025 automates.

Guard notes added at 025 plan stage (one line each, in
`neo4j_migration.py` + `graph_query.py`):

> "All pinned Cypher in this module runs against the session's
> database (the one the driver was built with, i.e. `KB_NEO4J__URL`'s
> `?db=` target, or `neo4j` if unset). A destructive legacy-label
> cleanup therefore runs inside the connected database — pin the URL
> explicitly when operating a tenant DB."

**Phase 2 — spec 025 (this document):** feature work that turns the
ops posture into the service model: `tenants` table, tenant lifecycle,
per-tenant DB + collection provisioning at `tenant create`,
copy-in-place backfill from the legacy shared collection,
tenant-partitioned state tables, per-tenant service tokens,
install-admin role. MAJOR bump. After 025 ships, the keyva-style
separate-box (A2) topology can be collapsed into tenant rows on a
shared box; A2 survives only for **untrusted** tenants where a
separate box (and its invariants I-1..I-7, "no cross-writes") is
required.

## Superseded scope (context, not re-litigation)

- **Q2 (BR-11.4.3)** locked v1 to soft isolation. This spec is the
  follow-up release it named; the decision it makes is *what shape*
  hard isolation takes — now fixed: **per-tenant hard boundary**
  (T1), with per-user scoping *within* a tenant remaining the
  003 `owner_tag` filter (A2 unchanged).
- **003-multi-user** (complete, v0.3.0) is the predecessor: role
  matrix (`ROLE_CAPS`), personal tokens, sessions, per-user
  `user_config`, `owner_tag` stamping, owner-scoped
  `can_access_schedule`. All continues to work; this spec adds a
  tenancy layer *around* it.
- **Constitution VI / NFR-15**: any schema change is additive-only
  (`apply_v6`, transactional, `user_version` advances by one); existing
  installs upgrade in place without data loss.

## Trust-tier model (blast-radius grounding)

The Qdrant↔Neo4j join key is `item_id` on `:SourceItem`
(`ingest/graph_query.py` L43/52; `mcp/dispatch.py` L889/926–929).
Chunk text lives in the **Qdrant payload**, not the graph. Therefore
a cross-tenant *graph* leak (structure + `item_id`s, no chunk text)
is **bounded** and acceptable for trusted tenants sharing one box
(A1-hard); chunk-text isolation is the Qdrant collection boundary,
which is hard by construction. This is exactly the tier the keyva
SOP (separate boxes qdrant-dt:6335 + neo4j-dt:7475/7689, invariants
I-1..I-7 "no cross-writes") already encodes — keyva is A2 until 025
collapses it. **Per-user hard DBs are out of scope** (strictly
heavier: one DB per 003 user, breaks the "one service = one KB,
multi-user ACLs on top" invariant 002/003 were built on).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Tenant is the isolation unit (Priority: P1)

An operator creates tenants (workspaces). Accounts belong to exactly
one tenant. Each tenant gets its own hard storage boundary: a
dedicated **Qdrant collection** (`kb-<slug>`) and a dedicated
**Neo4j database** (`<slug>`). A caller's session / personal token
resolves to that tenant's stores *before* any filter runs — no
`owner_tag` post-filter needed for isolation (it remains the
within-tenant user filter, A2).

**Why this priority**: this is the actual hard-isolation work Q2
deferred; everything else in this spec is plumbing around it.

**Independent Test**: Two tenants ingest the same source; each
tenant's query path returns only its own content even when the
caller *omits* the owner filter; a tenant-A token against tenant B's
surface is rejected (cross-tenant = 401, fail-closed).

**Acceptance Scenarios**:

1. **Given** two tenants, **When** both ingest the same source with
   the same items, **Then** each tenant's collection/DB contains only
   its own points/nodes; no cross-tenant point or node exists in
   either store.
2. **Given** tenant A's session token, **When** any query/mutation
   surface is used (web UI, HTTP API, MCP, CLI), **Then** it
   resolves to tenant A's collection + DB only; tenant B's content
   is unreachable from any surface without B's session.
3. **Given** an existing 003 install (soft isolation, shared
   collection), **When** the 025 migration runs, **Then** existing
   points copy in place (T2) into the install tenant's store without
   data loss; the legacy shared collection is retired from the write
   path after the point-count invariant passes.

### User Story 2 - Tenant-scoped state tables (Priority: P1)

The tenant boundary is threaded through every state table:
`highwater`, `audit_runs`/`audit_items`, `ingest_locks` are
tenant-partitioned so two tenants enabling the same source never
clobber each other's cursors, share a lock, or see each other's run
history (NFR-14, NFR-16). `user_config` and `schedules` remain
per-account within the tenant. Admin surfaces are tenant-scoped: a
tenant admin sees only their tenant's accounts/runs; the
install-level admin (T3) sees across tenants.

**Independent Test**: Two tenants schedule the same source/preset;
running both concurrently shows two independent highwater cursors,
two independent locks, and two independent audit trails; tenant A's
run-history view shows zero rows from tenant B.

**Acceptance Scenarios**:

1. **Given** tenants A and B both run source `hermes`, **When** both
   complete, **Then** `highwater` holds two rows partitioned by
   tenant; each reflects only its tenant's progress.
2. **Given** tenant A's admin, **When** they list runs/accounts,
   **Then** only tenant A rows appear; the install-level admin (T3)
   sees all.
3. **Given** one tenant's `ingest_locks` row for a source is held,
   **When** another tenant's schedule for the same source fires,
   **Then** it acquires its own lock and runs; no tenant-to-tenant
   lock contention (today `ingest_locks(source)` is install-global).

### User Story 3 - Tenant lifecycle (Priority: P2)

Tenants can be created, suspended, and (with confirmation + audit)
deleted; the slug is immutable in v1 (rename = store migration,
out of scope). Deletion drops the tenant's Qdrant collection,
**Neo4j database**, and tenant-partitioned state rows; suspension
makes all its auth tokens refuse service (403 suspended) without
deleting data. Provisioning is deterministic: collection created at
the pinned model dimension (007 S2-mismatch guard) **and database
created/verified** (Neo4j 5.x: `CREATE DATABASE <db>` then poll
`SHOW DATABASES` for `online`; the DB reaches `online` asynchronously
), so a fresh tenant's first run succeeds end-to-end (NFR-12).

**Independent Test**: `tenant create` then first run ingests into
the new stores; `tenant suspend` then every surface returns a named
"suspended" error; `tenant delete` then collection + DB dropped,
state rows gone, no orphaned points.

**Acceptance Scenarios**:

1. **Given** a new tenant, **When** its first scheduled run fires,
   **Then** the collection and DB are provisioned at the pinned
   dimension and the run completes.
2. **Given** a suspended tenant, **When** any signed-in caller of
   that tenant attempts a surface, **Then** they get a 403 with
   reason "tenant suspended" (no 500, no fallback to another tenant).
3. **Given** a tenant deletion request, **When** it is confirmed by
   an install admin, **Then** the tenant's Qdrant collection +
   Neo4j database are dropped and all tenant-partitioned state rows
   are removed; the audit trail records the event with the acting
   admin.

### User Story 4 - Tenant-scoped service tokens (Priority: P2)

The global `system` service token (003/BR-10) is split: each tenant
gets its own service token, and a token resolves to exactly one
tenant before any other check runs. Endpoint config stays
install-level (T4 = no per-tenant overrides); the 003 C-3
precedence chain is unchanged.

**Independent Test**: Tenant A's service token authenticates only A's
runs; presenting A's token to a tenant-B-only surface is 401;
revoking A's token does not affect B.

**Acceptance Scenarios**:

1. **Given** a per-tenant service token, **When** it authenticates,
   **Then** the caller is attributed to that tenant (and its owner
   within the tenant, per token owner).
2. **Given** the legacy shared `system` token, **When** it is used
   after 025 ships, **Then** it maps to the install-level admin
   tenant (T3) — not silently to every tenant.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-1** (supersedes BR-11.4.3): hard isolation per tenant —
  dedicated Qdrant collection (`kb-<slug>`) + dedicated Neo4j
  database (`<slug>`); the query/mutation path resolves the caller's
  stores *before* any filter; `owner_tag` post-filter becomes a
  within-tenant user filter, not the isolation boundary.
- **FR-2**: tenancy threads through `highwater`, `audit_runs`,
  `audit_items`, `ingest_locks` (new `tenant_id` partition key; see
  `data-model.md` DDL).
- **FR-3**: `tenants` table + tenant lifecycle (create/suspend/delete)
  with audit; provisioning auto-creates the collection **and**
  database at the pinned embedding dimension (DB creation is
  async in Neo4j 5.x — poll to `online` before first write).
- **FR-4**: token-to-tenant resolution: every auth surface (web
  session, personal token, service token, MCP, CLI `--as`) carries a
  tenant before role checks run; cross-tenant token use is rejected
  (401, fail-closed, no fall-through).
- **FR-5**: admin scoping per T3: **install-level admin** role
  crosses tenants; tenant admin does not. Endpoint config stays
  install-global (T4 = no per-tenant overrides; `tenant_config`
  NOT created in v1).
- **FR-6** (NFR-15): in-place migration for existing installs:
  additive DDL (`apply_v6`, `user_version` 5→6, see data-model.md),
  plus a one-shot **copy-in-place** backfill (T2) moving legacy
  shared-collection points into the install tenant's store; the
  legacy shared collection is retired from the write path only after
  backfill completes and the point-count invariant passes. Neo4j:
  legacy graph is copied into the install tenant's DB (node ids
  preserved); the default-DB graph remains read-only until cutover.
- **FR-7** (A1-hard guard): all pinned Cypher (013 suite,
  `neo4j_migration.py`, `graph_query.py`) MUST run against the
  session's database only; no `USE`/`dbms` cross-DB clause is
  introduced. Driver construction MUST pin the database via
  `KB_NEO4J__URL`'s `?db=` for tenant installs; a driver built
  without `?db=` targets the server default (`neo4j`) and is
  fail-fast-rejected on any tenant-partitioned write path (NFR-13
  companion guard, documented at plan stage).

### Key Entities

- **Tenant** — new: `id`, `slug` (immutable, `[a-z0-9-]`), `name`,
  `status` (active/suspended/deleted), `created_at`, `deleted_at`;
  owns `qdrant_collection = kb-<slug>` and `neo4j_database = <slug>`
  (both deterministic from `slug`).
- **Account** — 003 row extended with `tenant_id` FK (additive,
  default = install tenant).
- **TenantServiceToken** — per-tenant analogue of the shared
  `system` token.
- **Store mapping** — `tenant → (qdrant_collection,
  neo4j_database)`; deterministic, one lookup cached for the
  request duration; no per-row store lookup at query time.

### Out of scope

- Billing, multi-region, cross-tenant sync/replication, SSO/OAuth
  (already a follow-up in BR-11.4.1).
- **Per-user hard DBs** (sub-tenant DBs): strictly heavier — one DB
  per 003 user, breaks the "one service = one KB, multi-user ACLs on
  top" invariant 002/003 were built on. Within-tenant user scoping
  stays `owner_tag` (A2).
- Per-tenant LLM/embedding endpoint overrides (`tenant_config`) —
  follow-up feature (T4 deferred).
- Per-tenant separate-box hosting (A2) — ops posture, not a code
  feature; supported by the same 025 code (each tenant's URLs
  simply point at a separate box).
- Tenant rename (slug change ⇒ store migration) — follow-up.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: cross-tenant leak test — for every query surface
  (web, API, MCP, CLI), a tenant-A token returns zero rows from
  tenant B's store even when the owner filter is
  omitted/absent; automated per surface.
- **SC-002**: two tenants, same source, concurrent runs — independent
  `highwater`/`ingest_locks`/`audit` rows verified; zero cross-tenant
  lock contention (automated).
- **SC-003**: migration on a seeded legacy install (003-era data) —
  in-place upgrade passes with no data loss (point-count invariant
  per `owner_tag` bucket), NFR-15 automated check; Neo4j legacy DB
  node/edge counts match the install-tenant DB after copy.
- **SC-004**: tenant lifecycle — create→first-run green (DB reached
  `online` before first write); suspend→all surfaces 403
  "suspended"; delete→collection + DB dropped, state rows gone
  (automated).
- **SC-005**: 001/002 portability + one-record guards
  (`tests/integration/test_portability.py`, T006) stay green; NFR-1
  dedup still holds *within* a tenant.
- **SC-006**: zero host paths / install locations in new code
  (NFR-13, T027 knob-docs guard stays green).

## Assumptions

- **A1**: Qdrant and Neo4j instances remain install-level (one each)
  for trusted-tenant boxes; tenancy is achieved via collection +
  database partitioning, not per-tenant instances. Per-tenant
  instances (A2) remain a valid ops posture for untrusted tenants —
  the same 025 code supports both; only the URLs move.
- **A2**: `owner_tag` stamping (003) continues within a tenant so
  "only what I ingested" still works for multi-account tenants; the
  tag becomes a within-tenant filter, not the isolation boundary.
- **A3**: SQLite state DB stays the state store; tenancy is a
  partition-key change, not a database-per-tenant change.
- **A4 (superseded 2026-11-15)**: ~~Neo4j subgraph =
  label/naming-namespace convention on one instance~~. Replaced by:
  **per-tenant Neo4j database named `<slug>`** (T1). All pinned Cypher
  targets the session DB (FR-7 guard); no label prefix, no `USE`.
  **T5 refinement (2026-11-16):** on Neo4j Community edition the
  dedicated-DB boundary degrades to **channel-based partitioning**
  within the default `neo4j` database (Community has no multi-DB;
  `?db=` pinning and `CREATE DATABASE` are Enterprise-only). The
  `tenants.neo4j_database` field still stores the slug (Enterprise-
  ready); on Community the driver targets the default DB and the
  hard boundary is the channel taxonomy. FR-7 becomes Edition-
  conditional (Enterprise: `?db=` required; Community: channel
  scope required in the pinned query). A2 per-tenant instances
  remain the documented escape hatch, not the target state.
- **A5 (resolved)**: version bump is **MAJOR** — T1 changes the
  public store-resolution contract (store resolution becomes
  per-tenant).
- **A6 (new)**: Qdrant collection names are `kb-<tenant_slug>`; the
  pinned `personal_kb` (default single-tenant collection,
  `health.py` L18) and `mem0` (external) names are **reserved** and
  cannot be produced by slug expansion — the `kb-` prefix makes
  collision impossible (contract line, see data-model.md).

## Open verification items (recorded, non-blocking)

Items already checked against the codebase (2026-11-15 grep):
pinned Cypher in `graph_query.py` (L43/44/52) and
`neo4j_migration.py` (`_LEGACY_LABELS` loop, L39/43) contains no
`USE`/`dbms`/`CREATE DATABASE` — it targets the session DB by
construction, so `?db=` pinning is sufficient. §5.3 below is the
one remaining guard to add at plan stage.

- **§5.3 (plan-stage guard)**: confirm every driver-construction
  path (scheduler bootstrap, `run --once`, migration CLI, MCP server
  startup) builds its Neo4j driver from the resolved `KB_NEO4J__URL`
  *including* the `?db=` component; any path that constructs a
  driver from a URL lacking `?db=` on a tenant install MUST fail
  fast (not silently default to the `neo4j` database). One line of
  validation in `neo4j_migration.py` + a doc note in `graph_query.py`
  (see FR-7).

## Clarifications

All pre-003 decisions (Q1 to Q12) stand; nothing in this spec
re-opens BR-9.5, the role capability matrix, or the 003
soft-isolation *historical* behavior. T1–T5 are decided (above);
`plan → tasks` runs on the decided shape.
