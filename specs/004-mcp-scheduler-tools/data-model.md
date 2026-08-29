# Data Model: MCP Scheduler Tools (004)

004 **adds no tables and no schema migration.** It consumes the 001/002/003
schema (constitution VI / NFR-15: an in-place upgrade preserves state; 004's
upgrade path is a no-op because it creates nothing). This document maps each
004 entity onto the existing tables, states the read/write surface, and records
the one new pure helper (`can_access_schedule`) that 004 introduces.

## Tables consumed (unchanged)

| Table | From | 004 access | Columns 004 reads | Columns 004 writes |
|---|---|---|---|---|
| `schedules` | 002 DDL_V2 | read + write (via 002 `scheduler/schedules.py`) | `id, owner, source, preset, param, fire_time, enabled, next_fire_at, acl, created_at, updated_at` | `id, owner, source, preset, param, fire_time, enabled, next_fire_at, acl, created_at, updated_at` (via `create_schedule`/`update_schedule`/`delete_schedule`) |
| `audit_runs` | 001 DDL_V1 | read (`kb_run_history`) + write (real runs via `run_pipeline`; cross-user-read access-log row per R8) | `run_id, started_at, completed_at, status, trigger, scheduled_by, per_source_counts` | real run: all (pipeline-owned); cross-user read: `run_id` (fresh uuid4), `started_at`, `completed_at`, `status='ok'`, `trigger='mcp'`, `scheduled_by=<admin>`, `per_source_counts` (JSON with `mcp_history_query` block) |
| `accounts` | 001 DDL_V1 + 003 v3 cols | read-only (auth + role resolution) | `email, role` (via `accounts.get_role`) | — |
| `personal_tokens` | 003 DDL_V3 | read-only (auth) | via `auth.verify_personal_token` | — |
| `sessions` | 003 DDL_V3 | read-only (auth) | via `auth.verify_session` | — |

No `CREATE TABLE`, no `ALTER TABLE`, no new index. The `schedule.acl` column
(`TEXT NOT NULL DEFAULT 'owner'`) is **already in 002 DDL_V2** — 004 consumes
it (accepts it in create/update), it does not add it. Granular per-schedule ACL
*enforcement* is the documented follow-up (R5); the schema already supports it.

## Entity → table mapping

### MCP tool (an agent-facing surface, not a row)
- `kb_schedule_list`, `kb_schedule_create`, `kb_schedule_update`,
  `kb_schedule_delete`, `kb_schedule_run`, `kb_run_history` (the six, R2
  field names mirror 003 `contracts/scheduler.md`).
- `kb_search`, `kb_chat`, `kb_ingest`, `kb_health` (R10: registered but
  stubbed, return `code=not_implemented_yet`).
- Persisted **only** via the `audit_runs` row they write (a tool call itself
  has no row; the *effects* do).

### schedule (row in `schedules`)
- Agent-facing field names (R2) ↔ DB columns:
  | agent field | DB column | note |
  |---|---|---|
  | `schedule_id` | `schedules.id` | INTEGER PK surfaced to agents |
  | `owner` | `owner` | the schedule owner (email or `system`) |
  | `source` | `source` | a 001 source name |
  | `preset` | `preset` | CHECK-constrained: `daily`/`hourly`/`weekly`/`monthly`/`every-N-hours` |
  | `param` | `param` | the `every-N-hours` N; nullable otherwise |
  | `fire_time` | `fire_time` | `HH:MM` |
  | `enabled` | `enabled` | `0`/`1` |
  | `next_fire_at` | `next_fire_at` | UTC ISO-8601 |
  | `acl` | `acl` | reserved, default `'owner'`; accepted by create/update, **not** enforced in v1 (R5) |
  | `created_at` / `updated_at` | same | read fields |
- The `UNIQUE(owner, source, preset, param, fire_time)` constraint is enforced
  by 002's `create_schedule` (a duplicate raises `IntegrityError` → the tool
  maps it to `code=conflict`, see contracts). 004 adds no new constraint.

### audit record (row in `audit_runs`)
- Agent-facing field names (R2) ↔ DB columns:
  | agent field | DB column |
  |---|---|
  | `run_id` | `run_id` (TEXT PK) |
  | `started_at` | `started_at` |
  | `completed_at` | `completed_at` |
  | `status` | `status` (`ok`/`partial`/`failed`) |
  | `trigger` | `trigger` (`schedule`/`manual`/`mcp`/`api`) |
  | `scheduled_by` | `scheduled_by` (account email or `system`) |
  | `per_source_counts` | `per_source_counts` (JSON TEXT; also carries `agent_kind` + the `mcp_history_query` block, research D5/D6) |
- **`agent_kind`** is *not* a column — it is stored inside `per_source_counts`
  JSON (research D6). No schema change.

### can_access_schedule (new pure helper, NOT a table)
```python
def can_access_schedule(schedule_row: dict, caller_email: str,
                        caller_role: str) -> bool:
    """Owner-scoping check (R5/R9). True iff the caller owns the schedule
    or is admin. A future per-schedule ACL replaces this body, not the call
    sites (the `acl` column is already shipped)."""
    if schedule_row["owner"] == caller_email:
        return True
    return caller_role == "admin"   # R9: admin "list all" rides on the role
```
- Pure (no I/O), stateless. Enforces the caller-scoped default (R5) and the
  admin all-schedules visibility (R9). It is the single swappable choke point
  between every tool body and the owner/scope decision, so a future ACL is a
  one-function change.

## Invariants 004 must keep intact

1. **One-record-not-N (NFR-1, NFR-14).** `kb_schedule_run` routes through 001's
   `run_pipeline` (research D4); `owner`/`agent_kind` are payload/provenance
   fields that never enter the deterministic point ID. The same content
   ingested via schedule / `run --once` / MCP / web UI yields **one** point.
2. **Upgrade safety (NFR-15, constitution VI).** 004 adds no migration. An
   in-place upgrade from a 003 install to 004 preserves `.kbstate/`, the
   account DB, and config untouched.
3. **Auditability (constitution V).** Every `kb_schedule_run` writes exactly one
   pipeline-owned `audit_runs` row (`trigger='mcp'`); every admin
   cross-user `kb_run_history` read writes exactly one access-log row (R8);
   own-history reads write none.
4. **No host leakage (NFR-13, constitution I).** No table, index, or helper in
   004 names a host path, user, or install location. The DB is opened through
   the config-resolved state path (002's `_open_same_db` pattern).
5. **Credential non-leak (003 invariant 5).** 004 reads credential tables
   read-only; it never writes a token/session/password to any table.

## Read-only query helper note

004 needs **one** scoped read that 003's `list_runs` does not already provide
directly: *list schedules scoped by owner* (for `kb_schedule_list`). 002's
`list_schedules(db, owner=None)` already returns all-or-one-owner, so:
- `kb_schedule_list` (own scope) → `list_schedules(db, owner=caller_email)`,
  then filter by `can_access_schedule` (a no-op for the owner, a pass-through
  for admin).
- `kb_schedule_list` (admin all) → `list_schedules(db)` (all owners), gated by
  the `admin` role (R9).

No new table/view is required. The cross-user `kb_run_history` read reuses
003's `list_runs(db, user=<target>, all_users=True)` (admin path) or
`list_runs(db, user=caller_email)` (own path); the access-log row for the admin
cross-user read is a *write* to `audit_runs` (R8), not a new read surface.
