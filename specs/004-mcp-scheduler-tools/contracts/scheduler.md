# Contract: MCP Scheduler Tools (004)

The six scheduler tools + four BR-10 stubs, as MCP `tools/list` + `tools/call`
entries. Field names mirror 003 `contracts/scheduler.md` **exactly** (R2).
Every tool shares the same envelope, role-gate, owner-scope, and audit path
(research D2/D4/D5) — only the body differs.

## Shared envelope

**`tools/list`** returns ten tools: the six real (`kb_schedule_list`,
`kb_schedule_create`, `kb_schedule_update`, `kb_schedule_delete`,
`kb_schedule_run`, `kb_run_history`) + four stubs (`kb_search`, `kb_chat`,
`kb_ingest`, `kb_health`). The stubs are present so a fresh client sees a
complete, stable registry (R10); each returns `code=not_implemented_yet`.

**`tools/call` request** (per tool):
```json
{ "name": "<tool>", "arguments": { ...tool-specific..., "agent_kind": "<kind>" } }
```
- `agent_kind` is an **optional** top-level argument on *every* tool (R6).
  Value = the client's declared kind (e.g. `"hermes"`, `"claude-desktop"`) or
  `"unknown"` when absent. Recorded on the audit row (D5/D6).

**Success response** (MCP `result.content[0].text` is JSON):
```json
{ "ok": true, "data": { ...tool-specific... },
  "agent_kind": "<kind>" }
```

**Error response** (MCP tool error, machine-readable `code`, A4):
```json
{ "ok": false, "error": { "code": "<machine_code>", "message": "<human>" } }
```

**Error codes (stable, agents branch on these):**
| code | meaning | HTTP-ish | when |
|---|---|---|---|
| `unauthorized` | no / unknown credential | 401 | auth failed (D3) |
| `permission_denied` | role lacks the capability | 403 | R4 role-gate failed; `message` names the missing capability |
| `schedule_not_found` | target schedule id/owner doesn't exist (or caller can't see it) | 404 | owner-scope fails (R5) — *not* a "you can't see it" leak |
| `invalid_preset` | `preset` not one of the five | 400 | create/update |
| `invalid_param` | `param` not a positive int for `every-N-hours` (or present for a non-N preset) | 400 | create/update |
| `conflict` | duplicate `(owner,source,preset,param,fire_time)` | 409 | create |
| `source_disabled` | the schedule's source is not enabled | 409 | `kb_schedule_run` (nothing to run) |
| `run_failed` | `run_pipeline` ran and returned `failed` | 500 | `kb_schedule_run` (run audited as `failed`) |
| `internal_error` | unexpected | 500 | any |
| `not_implemented_yet` | BR-10 stub | 501 | the four stubs (R10) |

> `schedule_not_found` is deliberately used for *both* "doesn't exist" and
> "you can't see it" (owner-scoped, R5) so the response does not leak the
> existence of another user's schedule (SC-002 cross-user isolation).

**Role-gate + owner-scope ordering (every tool, R4/R5/R9):**
1. Authenticate → `(caller_email, caller_role)` (D3). On failure → `unauthorized`.
2. Role-gate the tool's capability (below) via `accounts.require_capability`.
   On denial → `permission_denied` (message names the capability).
3. Owner-scope the target schedule via `can_access_schedule` (R5/R9).
   On failure → `schedule_not_found`.
4. Run the body; write the audit row (if applicable, D4/D5); return.

## The six tools

### 1. `kb_schedule_list`
- **Role-gate:** `query_status` (own) — R4.
- **Args (in addition to `agent_kind`):**
  - `all_users: bool = false` — `true` only honored for an `admin` caller (R9); a non-admin requesting it gets `permission_denied` (message: needs admin).
- **Behavior:**
  - Own scope (`all_users=false`): `list_schedules(db, owner=caller_email)` → filter by `can_access_schedule` (a no-op for owner).
  - Admin all (`all_users=true`, `caller_role='admin'`): `list_schedules(db)` → all owners.
- **Return `data`:** `{ "schedules": [ {schedule object}, ... ] }` where a schedule object is:
  `{ "schedule_id", "owner", "source", "preset", "param", "fire_time", "enabled", "next_fire_at", "acl", "created_at", "updated_at" }` (R2; `schedule_id` = `schedules.id`).
- **Audit:** **none** (a read of the caller's own schedules; not a run).

### 2. `kb_schedule_create`
- **Role-gate:** `schedule_crud` (R4).
- **Args:** `source` (str, required), `preset` (str, required, one of five), `param` (int, optional — required iff `preset='every-N-hours'`), `fire_time` (str `"HH:MM"`, default `"03:00"`), `enabled` (bool, default `true`), `acl` (str, default `"owner"` — accepted, stored, **not enforced** in v1, R5). Plus `agent_kind`.
- **Behavior:** `create_schedule(db, owner=caller_email, source, preset, param, fire_time, ...)` (002 helper; `owner` is always the caller in v1 — caller-scoped, R5). `next_fire_at` is computed by 002's `expand_next`.
- **Errors:** `invalid_preset`, `invalid_param`, `conflict` (duplicate tuple), `permission_denied`.
- **Return `data`:** `{ "schedule": {<schedule object>} }` (the new row).
- **Audit:** **none** (a schedule mutation is not a run; the audit trail of
  *runs* is `audit_runs`. The mutation itself is logged via structured logging,
  constitution V — not an `audit_runs` row).

### 3. `kb_schedule_update`
- **Role-gate:** `schedule_crud` (R4).
- **Args:** `schedule_id` (int, required) + any of `preset`, `param`, `fire_time`, `enabled`, `acl`. Plus `agent_kind`.
- **Behavior:** fetch the schedule (owner-scoped via `can_access_schedule` → `schedule_not_found` if not), then `update_schedule(db, schedule_id, **fields)` (002). Recompute `next_fire_at` on preset/param/fire_time change.
- **Errors:** `schedule_not_found`, `invalid_preset`, `invalid_param`, `permission_denied`.
- **Return `data`:** `{ "schedule": {<schedule object>} }`.
- **Audit:** **none**.

### 4. `kb_schedule_delete`
- **Role-gate:** `schedule_crud` (R4).
- **Args:** `schedule_id` (int, required). Plus `agent_kind`.
- **Behavior:** fetch + owner-scope, then `delete_schedule(db, schedule_id)` (002).
- **Errors:** `schedule_not_found`, `permission_denied`.
- **Return `data`:** `{ "deleted": <schedule_id> }`.
- **Audit:** **none**.

### 5. `kb_schedule_run`
- **Role-gate:** `trigger_run` (R4).
- **Args:** `schedule_id` (int, required). Plus `agent_kind`.
- **Behavior (research D4 — pipeline parity):**
  1. Fetch the schedule (owner-scoped → `schedule_not_found` if not).
  2. If the schedule's source is disabled → `source_disabled`.
  3. `merged = merge_user_config(config, db, schedule["owner"], source=schedule["source"])` (003).
  4. `run_pipeline(merged, db, qdrant_factory, embedder, source_names=[schedule["source"]], trigger="mcp", scheduled_by=caller_email, owner=schedule["owner"])` (001 — the SAME pipeline every trigger path uses; one-record-not-N holds).
  5. Stamp `agent_kind` onto that run's `per_source_counts` JSON (D6), same transaction window.
  6. Return the run record.
- **Errors:** `schedule_not_found`, `source_disabled`, `run_failed` (pipeline returned `failed`; the row is audited as `failed` with `trigger='mcp'`), `permission_denied`.
- **Return `data`:**
  `{ "run_id", "status" (ok|partial|failed), "per_source_counts": {source: n}, "agent_kind" }`.
- **Audit:** **one** pipeline-owned `audit_runs` row — `trigger='mcp'`,
  `scheduled_by=<caller>`, `status` = pipeline outcome, `per_source_counts`
  = real counts + `agent_kind` (D4/D5). This is the row the top acceptance
  check (NFR-1/NFR-14) dedups against the CLI/schedule/web-UI runs.

### 6. `kb_run_history`
- **Role-gate:** `view_own_history` (own) **or** `view_all_history` (all, admin-only, R4/R3).
- **Args:**
  - `user: str | None` — target account email. Omitted ⇒ the caller's own runs (own scope). Set to *another* user's email ⇒ cross-user (requires `view_all_history` ⇒ `permission_denied` for non-admin).
  - `source: str | None` — optional filter on the run's source.
  - `since: str | None` (UTC ISO-8601), `until: str | None` — optional time range on `started_at`.
  - `limit: int` (default 100, max 1000).
  - Plus `agent_kind`.
- **Behavior:**
  - Own scope (`user` omitted or `user == caller_email`): `list_runs(db, user=caller_email)` (003) → filter by `source`/`since`/`until`/`limit`. **No audit row** (the normal "mine" view).
  - Admin cross-user (`user != caller_email`, `caller_role='admin'`): `list_runs(db, user=<target>, all_users=True)` (003) → filter. **Writes one access-log row** (R8):
    - `run_id` = fresh `uuid.uuid4()`
    - `started_at`/`completed_at` = now
    - `status='ok'`
    - `trigger='mcp'`
    - `scheduled_by=<admin caller_email>`
    - `per_source_counts` = `{"mcp_history_query": {"target_user": <target>, "agent_kind": <kind>}}`
- **Errors:** `permission_denied` (non-admin cross-user), `unauthorized`.
- **Return `data`:**
  `{ "runs": [ { "run_id","started_at","completed_at","status","trigger","scheduled_by","per_source_counts" }, ... ], "count": N }` (R2 field names).
- **Audit:** none for own; **one** access-log row for an admin cross-user read
  (R8). One read = one row; repeated reads = repeated rows (documented).

## The four BR-10 stubs (R10)

All four are registered in `tools/list` (so a fresh client sees a complete
registry) but each body immediately returns:
```json
{ "ok": false, "error": { "code": "not_implemented_yet",
  "message": "kb_<name> is a BR-10 tool, a follow-up slice; not implemented in 004" } }
```
- `kb_search`, `kb_chat`, `kb_ingest`, `kb_health` — **no role-gate, no owner
  scope, no audit row** (they do nothing). They are documented as follow-ups in
  `docs/references/agent-guides.md` (FR-3).
- Their `inputSchema` is a minimal placeholder (`{}` or the eventual arg
  shape) so the registry is honest and stable; the bodies never touch the DB.

## Parity assertion (R7, SC-001)

For a fixed `(MCPContext, tool, args)`, the `dispatch` output is
transport-independent: stdio and HTTP/SSE both call the same
`build_tool_registry` + `dispatch` (research D2). The SC-001 contract test
asserts byte-identical `dispatch` results across both transports for the same
`ctx` + `args` — passing by construction because there is one registry and one
executor.

## Invariants (004)
1. **R2 field names** are the stable agent-facing contract; a future ACL must
   not rename them.
2. **One-record-not-N:** `kb_schedule_run` routes through 001 `run_pipeline`;
   `owner`/`agent_kind` are payload/provenance, never in the point ID.
3. **One audit row per run** (`kb_schedule_run`) and **one access-log row per
   admin cross-user read** (`kb_run_history`); own reads and schedule
   mutations write no `audit_runs` row.
4. **Owner-scoping by default** (R5): every target read goes through
   `can_access_schedule`; cross-user is admin-only (R9).
5. **No schema change:** 004 adds no table, no column, no migration.
