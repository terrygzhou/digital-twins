# MCP Contract - 025-multi-tenant

## Changelog (additions to the 003/004 MCP surface)

All existing tools are unchanged in signature. The new behaviour is
in the *resolution layer* below the tool bodies:

1. Authentication (mcp/auth.py) now returns a (tenant_id,
   account_email) pair, not just account_email. The shared system
   token maps to the install tenant (T3).
2. Every tool body that resolves a Qdrant collection or Neo4j
   database now takes tenant_id and looks up the store mapping
   from the tenants row (data-model.md). The Neo4j driver is
   built from the resolved KB_NEO4J__URL including the ?db=
   component on Enterprise; on Community the driver targets the
   default neo4j DB and the channel taxonomy is the hard boundary
   (T5). A driver lacking ?db= on Enterprise is fail-fast-rejected
   on any tenant-partitioned write path; on Community the guard
   verifies the pinned query carries the tenant's channel scope
   (FR-7, Edition-conditional).
3. kb_schedule_* and kb_run_history apply a tenant filter on their
   state-table queries (FR-2); can_access_schedule (mcp/acl.py)
   gains a same-tenant predicate before the owner/admin check.

## New MCP tools

| Tool | Purpose |
|---|---|
| kb_tenant_list | List tenants (install-level admin only, T3) |
| kb_tenant_create | Create + provision a tenant (install admin) |
| kb_tenant_suspend / kb_tenant_delete | Lifecycle (install admin) |
| kb_tenant_service_token | Issue/revoke per-tenant service token (install admin) |

## As-built deviations

The following MCP tools are NOT yet registered as MCP tools (CLI/Web only,
deviation from the original contract above):

- `kb_tenant_list`, `kb_tenant_create`, `kb_tenant_suspend`,
  `kb_tenant_delete`, `kb_tenant_service_token` — lifecycle management
  is exposed via the `digital-twins tenant` CLI group and the
  `/api/tenants` web endpoints, not as MCP tools. The auth-layer
  tenant resolution (contract item 1) and state-table tenant filter
  (contract item 3) are implemented as specified.

## Error contract

- Cross-tenant token use: 401 (auth layer) - the token is valid but
  scoped to a different tenant than the one the surface is
  serving. No fall-through to another tenant.
- Suspended tenant: 403 with reason "tenant suspended".
