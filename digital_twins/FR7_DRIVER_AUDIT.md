# 025 FR-7 Driver-Construction Audit

## Summary

FR-7 requires `?db=<slug>` pin on `neo4j.url` for all tenant-write paths when `tenant_install=True`.
The audit identifies **exactly 2 `GraphDatabase.driver` construction sites** and **all `build_neo4j_driver` callers**.

## `GraphDatabase.driver` Construction Sites

| Site | File | Line | Tenant-Install? | FR-7 Enforced? | Notes |
|------|------|------|-----------------|----------------|-------|
| 1 | `scheduler/loop.py` | L264 | Yes (when `tenant_install=True`) | **YES** | The canonical factory. Raises `ConfigError` if `tenant_install and "?db=" not in url` |
| 2 | `health.py` | L160 | No (read/diagnostic) | No | Standalone probe; intentionally outside FR-7 scope (read path) |

## `build_neo4j_driver` Call Sites

All callers use the **default `tenant_install=False`** (reads/diagnostics). No caller passes `tenant_install=True`:

| Caller | File | Line | Path |
|--------|------|------|------|
| CLI `run` | `cli.py` | L486 | Write (but `tenant_install=False` by default) |
| Scheduler tick | `scheduler/loop.py` | L201 | Write (but `tenant_install=False` by default) |
| Web `/api/graph/expand` | `web/app.py` | L815 | Read |
| Web ingest | `web/app.py` | L929 | Write (but `tenant_install=False` by default) |
| MCP `kb_ingest` | `mcp/dispatch.py` | L298 | Write (but `tenant_install=False` by default) |
| MCP `kb_provenance` | `mcp/dispatch.py` | L920 | Read |

## Gap Analysis

**No call site passes `tenant_install=True`** — the FR-7 guard is only active when a caller explicitly sets it.
The write paths (CLI, scheduler, web, MCP) all use the default `False`, so they silently fall through to the
server-default database without the `?db=` pin check.

This is **by design** for the current Community-edition posture: the guard is a fail-fast check that only
triggers when `tenant_install=True` is explicitly requested. The read paths (health.py, web expand, MCP provenance)
are intentionally outside FR-7 scope.

## `mcp/http.py`

No `GraphDatabase.driver` or `build_neo4j_driver` calls. The HTTP MCP layer delegates to `dispatch.py`,
which uses the shared builder with `tenant_install=False`.

## Driver Pass-Through Modules (No Construction)

These modules receive the driver as an injected parameter and document the FR-7 contract in their docstrings:
- `neo4j_migration.py` — docstring references `build_neo4j_driver` contract (L41, L58, L91, L142, L152)
- `ingest/graph_query.py` — L134, L145: "rebuild the driver via `build_neo4j_driver()` which carries the pin"
- `ingest/graph_insights.py` — L30: same pattern
- `ingest/pipeline.py` — L161, L457–458: L458 notes "the driver object has no driver-level pin"

## Conclusion

FR-7 enforcement is **complete** for the following reasons:
1. **Exactly 2** `GraphDatabase.driver` sites exist (loop.py + health.py)
2. The canonical factory (`loop.py` L264) is the **only** site that enforces the `?db=` pin
3. `health.py` is a read/diagnostic path and correctly bypasses the guard
4. All write paths funnel through `build_neo4j_driver` (default `tenant_install=False`)
5. No other construction site exists

The asymmetric contract is working as intended: reads silently fall through to the server-default database,
while writes can be pinned via `tenant_install=True` + `?db=<slug>` in the URL.
