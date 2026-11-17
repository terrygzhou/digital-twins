# Change: 025 — Multi-Tenant Data Isolation (hard per-tenant storage boundary)

## Why
003 shipped *soft* isolation: a shared Qdrant collection + a shared Neo4j
graph, with per-user scoping done by an `owner_tag` **post-filter**. That
design is a trust boundary in code, not in storage — a post-filter bug (or a
query that forgets the filter) leaks across users. The deferral decision in
requirement.md §5 (Q2: "hard per-user later", DECIDED 2026-08-29) was
deliberately parked until the store-resolution contract could be changed
without breaking install assumptions; this change is that follow-through.

The A1-hard tenancy decision recorded in
`digital-twins-migration/docs/architecture/HANDOFF.md` (2026-11-15) makes
the hard boundary operational: on the shared box, trusted tenants get a
hard Qdrant collection boundary (`kb-<slug>`) **today**, with the Neo4j
boundary degraded to channel-based partitioning inside the default `neo4j`
database because the live box runs Neo4j 5.26 **Community** edition
(multi-database — `CREATE DATABASE`, `?db=` pinning — is Enterprise-only).
Spec 025 is what *automates* that posture across every state table,
auth surface, and MCP/CLI tool.

025 is a **MAJOR** release (A5): the store-resolution contract changes
from install-global to per-tenant, so consumers (keyva-ontology in
particular) must plan their cutover around it. Its keyva cutover is
explicitly gated on this change shipping.

## What changes
1. **Per-tenant hard boundary (T1).** A dedicated Qdrant collection
   (`kb-<slug>`) **and** a dedicated Neo4j database (`<slug>`) per
   tenant — not per user. Neo4j 5.x multi-database gives a
   storage-layer hard boundary for ~zero code (the driver URL gets
   `?db=<tenant_db>`); Qdrant's native boundary is the collection.
   One service instance serves all tenants; the trust tier moves with
   ops, not the data model.

2. **Copy-in-place backfill (T2).** `qdrant_client.upload_points`
   (payload-preserving, **no re-embedding**). The kb-pk backfill source
   is the 6335 `personal_kb` copy (~4.5M pts, post-reembed,
   `item_key`-indexed); the target is `kb-kb-pk`. The legacy shared
   collection + legacy default-DB graph retire from the write path only
   after backfill completes and the point-count invariant passes
   (SC-003, the 7-day rollback window).

3. **Install-level admin role + per-tenant service tokens (T3).** The
   legacy `admin` role migrates to an install-level admin; a
   bootstrap/root token maps to it. Tenant admins cannot cross
   tenants; install admins can (account management, tenant lifecycle,
   cross-tenant run history). Per-tenant service tokens use the C-1
   hash pattern.

4. **No per-tenant endpoint overrides in v1 (T4).** The `tenant_config`
   table is **not** created. LLM/embedding/Qdrant/Neo4j endpoints
   stay install-global (NFR-13). Per-tenant overrides are a
   follow-up feature; the C-3 precedence chain stays at 3 levels.

5. **Edition-conditional FR-7 guard (T5).** Neo4j Community 5.26 has
   no multi-DB: `tenants.neo4j_database` stores the slug
   (Enterprise-ready) but the Community driver targets the default
   `neo4j` DB and isolates by **channel taxonomy** (keyva channels
   `keyva*`/`fs` vs all others = kb-pk). The FR-7 fail-fast guard is
   Edition-conditional:
   - **Enterprise** — a driver lacking `?db=` on a
     tenant-partitioned write path is rejected fail-fast.
   - **Community** — the guard verifies the pinned query carries the
     tenant's channel scope; a query without channel scope on a tenant
     install is rejected.
   Upgrading the box to Enterprise activates the `?db=` posture
   without code changes. Per-tenant Neo4j instances (the A2 pattern)
   remain a documented escape hatch, not the target state. OSS
   alternatives (FalkorDB/Memgraph) are rejected: they break the
   pinned-Cypher session-API contract and the 013 ADR.

6. **Additive-only migration v6 (NFR-15 / Constitution VI).**
   `apply_v6` (`user_version` 5→6, transactional): new `tenants`
   table; `tenant_id` column on `accounts`, `audit_runs`,
   `audit_items`; PK extension on `highwater` / `ingest_locks`
   to `(tenant_id, source, item_key)` / `(tenant_id, source)` via an
   in-place table swap in one transaction; new
   `tenant_service_tokens`. The install tenant is seeded as id=1
   with the empty-string legacy shared store pair `("", "")` — the
   T2-backfill bridge, **not** `kb-install`/`install`.

7. **Resolution + lifecycle layer.** New `tenant_service` module:
   `resolve_stores(conn, tenant_id)` (one SELECT per request, no
   in-module caching) and `provision_tenant` / `suspend_tenant` /
   `delete_tenant`. Store-side effects (Qdrant `CREATE/DELETE
   COLLECTION`, Neo4j `CREATE/DROP DATABASE`) are deferred to the
   provision CLI slice.

8. **MCP + CLI surfaces.** Auth returns `(tenant_id, email)`; new
   `kb_tenant_*` MCP tools; cross-tenant = 401, suspended = 403
   (`contracts/mcp.md`). CLI `digital-twins tenant
   list|create|suspend|delete|service-token` (`contracts/cli.md`).

## Non-goals
- No per-tenant endpoint overrides in v1 (T4; `tenant_config` not
  created).
- No per-tenant Neo4j instances as the target state (A2 remains a
  documented escape hatch, not the design).
- No per-tenant OSS graph databases (FalkorDB/Memgraph rejected).
- No label-prefix Neo4j boundary (A4 superseded by the DB-name
  boundary).
- No rewrite or drop of legacy data rows (additive-only, NFR-15).

## Impact
- `digital_twins/state/migrations.py` (`apply_v6`, `user_version`
  5→6, transactional)
- `digital_twins/state/models.py` (v6 schema model)
- `digital_twins/tenant_service.py` (new: `resolve_stores`,
  `provision_tenant`, `suspend_tenant`, `delete_tenant`, named
  exceptions)
- `digital_twins/graph_query.py` + `digital_twins/neo4j_migration.py`
  (FR-7 one-line guard notes at plan stage; full guard T028–T030)
- `digital_twins/auth.py` / `registry.py` / `dispatch.py` (MCP
  wiring: `(tenant_id, email)` auth return, new `kb_tenant_*` tools,
  401/403 mapping)
- `digital_twins/cli.py` (new `tenant` subcommand)
- `digital_twins/backfill.py` (T2 copy-in-place, payload-only)
- `tests/integration/test_multi_tenant.py` (new cross-surface leak
  suite; planned)
- Semver: **MAJOR** (`__version__` → `1.0.0`, A5) + CHANGELOG note:
  "store-resolution contract is per-tenant, not install-global"
