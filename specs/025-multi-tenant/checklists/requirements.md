# Requirements Checklist - 025-multi-tenant

## Functional requirements

- [ ] FR-1: per-tenant hard isolation (Qdrant collection + Neo4j database); owner_tag demoted to within-tenant filter
- [ ] FR-2: highwater / audit_runs / audit_items / ingest_locks carry tenant_id
- [ ] FR-3: tenants table + lifecycle (create/suspend/delete) + provisioning
- [ ] FR-4: token-to-tenant resolution on every surface; cross-tenant use rejected
- [ ] FR-5: admin scoping per T3; install-level config stays global (T4 = no per-tenant overrides in v1; tenant_config NOT created)
- [ ] FR-6: additive apply_v6 migration + copy-in-place backfill (T2) + legacy retirement
- [ ] FR-7: pinned Cypher runs against session DB only; driver URL MUST include ?db= for tenant installs (fail-fast guard)

## Non-functional invariants

- [ ] NFR-1: one-record dedup still holds *within* a tenant (T006 guard)
- [ ] NFR-13: zero host paths / install locations in new code (T027 guard)
- [ ] NFR-14: idempotent scheduling - two tenants, same source, no clobber (SC-002)
- [ ] NFR-15: in-place upgrade, no data loss (SC-003)
- [ ] NFR-16: per-tenant run-history audit (tenant admin sees own tenant; install admin sees all, per T3)

## Success criteria

- [ ] SC-001: cross-tenant leak test per surface (web / API / MCP / CLI)
- [ ] SC-002: two tenants same source concurrent runs - independent rows
- [ ] SC-003: seeded legacy install migrates with point-count invariant
- [ ] SC-004: lifecycle create->green / suspend->403 / delete->gone
- [ ] SC-005: 001/002 portability + one-record guards green
- [ ] SC-006: portability / knob-docs guards green
