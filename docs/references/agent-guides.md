# Agent Guides: Onboarding Any MCP Agent to the Scheduler Tools

This is the canonical onboarding page for any MCP-capable agent (Hermes,
Claude Desktop, Cursor, pi, DSH, or a bespoke client) that wants to manage
schedules and trigger runs against a running `kb-mcp` server. It documents the
six 004 scheduler tools, the four BR-10 stubs, the shared success/error
envelope, the stable error codes, the role requirements, and the transport.

The field names here mirror the 003/004 contract
(`specs/004-mcp-scheduler-tools/contracts/scheduler.md`) exactly; they are the
stable agent-facing contract (004 ruling R2) and a future ACL must not rename
them. Nothing in this page is a host-specific constant: no host paths, no
interpreter pins, no literal usernames — every example uses a placeholder. The
token is the only host-specific thing an agent is handed.

## 1. Onboarding

An agent with **one personal token** and **one transport URL** gets the full
tool set. No host-specific setup beyond the token (BR-11.5.2, NFR-13):

- **Token** — a personal token from the owner's `kb-mcp` (003's
  `personal_tokens` table; the shared service token is a separate BR-10
  credential, not per-user). The token is the only host-specific thing the
  agent is handed.
- **Transport URL** — either `stdio` (the agent spawns the `kb-mcp` process)
  or `http+sse` (the agent points at a running `kb-mcp` endpoint). See §5.

With the token + transport URL, the agent:

1. Authenticates (the token resolves to `(caller_email, caller_role)`).
2. Lists tools (`tools/list`) — sees all ten: the six scheduler tools + the
   four BR-10 stubs.
3. Calls tools (`tools/call`) — every mutating call is role-gated; a denied
   capability returns `code=permission_denied` naming the missing
   capability.

A `reader` may list schedules and read its own run history, but every mutating
tool is denied with `code=permission_denied`. A `scheduler` may manage its own
schedules and trigger runs. An `admin` additionally sees other users' data
(cross-user reads) and may set `all_users=true` on the list tool.

## 2. The shared success / error envelope

Every tool shares the same envelope (004 ruling; A4 machine-readable codes).
The `agent_kind` argument is optional on **every** tool and is recorded on the
audit row when present.

### Success

MCP `result.content[0].text` is JSON:

```json
{ "ok": true, "data": { ...tool-specific... }, "agent_kind": "<kind>" }
```

### Error

MCP tool error, machine-readable `code` (agents branch on these):

```json
{ "ok": false, "error": { "code": "<machine_code>", "message": "<human>" } }
```

`agent_kind` is the client's declared kind (e.g. `"hermes"`,
`"claude-desktop"`) or `"unknown"` when absent.

## 3. The six 004 scheduler tools

The **role-gate + owner-scope ordering** is identical for every tool:

1. Authenticate → `(caller_email, caller_role)`. Failure → `unauthorized`.
2. Role-gate the tool's capability. Denial → `permission_denied` (message
   names the missing capability).
3. Owner-scope the target schedule via `can_access_schedule`. Failure →
   `schedule_not_found`.
4. Run the body; write the audit row (if applicable); return.

The schedule object returned by the tools carries: `schedule_id`, `owner`,
`source`, `preset`, `param`, `fire_time`, `enabled`, `next_fire_at`, `acl`,
`created_at`, `updated_at` (003/004 R2 field names; `schedule_id` is the
`id` column).

### 3.1 `kb_schedule_list`

- **Role requirement:** `query_status` (own scope).
- **Args (beyond `agent_kind`):**
  - `all_users: bool = false` — honored only for an `admin` caller; a
    non-admin requesting it gets `permission_denied` (message: needs admin).
- **Behavior:** lists the caller's own schedules (owner-scoped); with
  `all_users=true` and `caller_role=admin`, lists all owners' schedules.
- **Success `data`:** `{ "schedules": [ {schedule object}, ... ] }`
- **Audit:** none (a read of the caller's own schedules is not a run).
- **Errors:** `unauthorized`, `permission_denied` (non-admin `all_users=true`).

### 3.2 `kb_schedule_create`

- **Role requirement:** `schedule_crud`.
- **Args (beyond `agent_kind`):**
  - `source` (str, required) — the source name to ingest.
  - `preset` (str, required) — one of the five: `daily`, `weekly`,
    `every-N-hours`, `once`, `manual`.
  - `param` (int, optional) — required iff `preset='every-N-hours'`; must be a
    positive int. Present for a non-N preset → `invalid_param`.
  - `fire_time` (str `"HH:MM"`, default `"03:00"`).
  - `enabled` (bool, default `true`).
  - `acl` (str, default `"owner"` — accepted and stored, **not enforced** in
    v1; the per-schedule ACL is a reserved placeholder, Q9).
- **Behavior:** creates a schedule owned by the caller (owner is always the
  caller in v1 — caller-scoped). `next_fire_at` is computed by 002's
  `expand_next`.
- **Success `data`:** `{ "schedule": {<schedule object>} }` (the new row).
- **Audit:** none (a schedule mutation is not a run; mutations are logged via
  structured logging, not an `audit_runs` row).
- **Errors:** `invalid_preset`, `invalid_param`, `conflict` (duplicate
  `(owner, source, preset, param, fire_time)` tuple), `permission_denied`,
  `unauthorized`.

### 3.3 `kb_schedule_update`

- **Role requirement:** `schedule_crud`.
- **Args (beyond `agent_kind`):**
  - `schedule_id` (int, required) — the target schedule.
  - plus any of: `preset`, `param`, `fire_time`, `enabled`, `acl`.
- **Behavior:** fetches the schedule (owner-scoped → `schedule_not_found` if
  not visible), then updates the supplied fields. Recomputes `next_fire_at` on
  `preset` / `param` / `fire_time` change.
- **Success `data`:** `{ "schedule": {<schedule object>} }`.
- **Audit:** none.
- **Errors:** `schedule_not_found`, `invalid_preset`, `invalid_param`,
  `permission_denied`, `unauthorized`.

### 3.4 `kb_schedule_delete`

- **Role requirement:** `schedule_crud`.
- **Args (beyond `agent_kind`):**
  - `schedule_id` (int, required).
- **Behavior:** fetches + owner-scopes the schedule, then deletes it.
- **Success `data`:** `{ "deleted": <schedule_id> }`.
- **Audit:** none.
- **Errors:** `schedule_not_found`, `permission_denied`, `unauthorized`.

### 3.5 `kb_schedule_run`

- **Role requirement:** `trigger_run`.
- **Args (beyond `agent_kind`):**
  - `schedule_id` (int, required) — the schedule to fire now.
- **Behavior (pipeline parity):** fetches the schedule (owner-scoped); if the
  schedule's source is disabled → `source_disabled`; merges the owner's
  user-config overrides into the config; runs the **same** `run_pipeline`
  every trigger path uses (`trigger="mcp"`, `scheduled_by=<caller>`,
  `owner=<schedule owner>`), so the one-record-not-N invariant holds; stamps
  `agent_kind` onto the run's `per_source_counts`; returns the run record.
- **Success `data`:**
  `{ "run_id", "status" (ok|partial|failed), "per_source_counts": {source: n},
  "agent_kind" }`.
- **Audit:** **one** pipeline-owned `audit_runs` row — `trigger='mcp'`,
  `scheduled_by=<caller>`, `status` = pipeline outcome, `per_source_counts` =
  real counts + `agent_kind`. This is the row the top acceptance check
  (NFR-1/NFR-14) dedups against CLI / schedule / web-UI runs.
- **Errors:** `schedule_not_found`, `source_disabled`, `run_failed` (pipeline
  returned `failed`; the row is audited as `failed` with `trigger='mcp'`),
  `permission_denied`, `unauthorized`.

### 3.6 `kb_run_history`

- **Role requirement:** `view_own_history` (own scope) **or**
  `view_all_history` (all, admin-only).
- **Args (beyond `agent_kind`):**
  - `user` (str | None) — target account email. Omitted ⇒ the caller's own
    runs (own scope). Set to another user's email ⇒ cross-user (requires
    `view_all_history`; a non-admin gets `permission_denied`).
  - `source` (str | None) — optional filter on the run's source.
  - `since` (str | None, UTC ISO-8601) — optional time range on `started_at`.
  - `until` (str | None) — optional time range on `started_at`.
  - `limit` (int, default 100, max 1000).
- **Behavior:** own scope lists the caller's runs; an admin cross-user read
  writes one access-log row (`trigger='mcp'`, `scheduled_by=<admin>`,
  `per_source_counts` = `{"mcp_history_query": {"target_user": <target>,
  "agent_kind": <kind>}}`).
- **Success `data`:**
  `{ "runs": [ { "run_id", "started_at", "completed_at", "status", "trigger",
  "scheduled_by", "per_source_counts" }, ... ], "count": N }`.
- **Audit:** none for own; one access-log row for an admin cross-user read.
- **Errors:** `permission_denied` (non-admin cross-user), `unauthorized`.

### Role requirement summary

| Tool | Capability required |
|---|---|
| `kb_schedule_list` | `query_status` (own) |
| `kb_schedule_create` | `schedule_crud` |
| `kb_schedule_update` | `schedule_crud` |
| `kb_schedule_delete` | `schedule_crud` |
| `kb_schedule_run` | `trigger_run` |
| `kb_run_history` | `view_own_history` (own) / `view_all_history` (all, admin) |

A **`reader`** may `kb_schedule_list` (own) + `kb_run_history` (own), but is
denied every mutating tool (`kb_schedule_create`, `kb_schedule_update`,
`kb_schedule_delete`, `kb_schedule_run`) with `code=permission_denied`, whose
`message` names the missing capability.

## 4. The four stubbed BR-10 tools

All four are **registered** in `tools/list` (so a fresh client sees a complete,
stable registry) but are **stubbed** in 004. Each body immediately returns:

```json
{ "ok": false, "error": { "code": "not_implemented_yet",
  "message": "kb_<name> is a BR-10 tool, a follow-up slice; not implemented in 004" } }
```

- `kb_search` — registered-but-stubbed (BR-10 follow-up slice).
- `kb_chat` — registered-but-stubbed (BR-10 follow-up slice).
- `kb_ingest` — registered-but-stubbed (BR-10 follow-up slice).
- `kb_health` — registered-but-stubbed (BR-10 follow-up slice).

These have **no role-gate, no owner-scope, no audit row** — they do nothing.
They are documented so a fresh client knows which tools are real (the six
scheduler tools above) vs. follow-up (the four BR-10 stubs). Their
`inputSchema` is a minimal placeholder; the bodies never touch the DB.

## 5. Stable error codes

The full set of stable error codes agents branch on (004, machine-readable,
A4):

| code | meaning | when |
|---|---|---|
| `unauthorized` | no / unknown credential | auth failed |
| `permission_denied` | role lacks the capability | role-gate failed; `message` names the missing capability |
| `schedule_not_found` | target schedule id/owner doesn't exist (or caller can't see it) | owner-scope fails — *not* a "you can't see it" leak |
| `invalid_preset` | `preset` not one of the five | create/update |
| `invalid_param` | `param` not a positive int for `every-N-hours` (or present for a non-N preset) | create/update |
| `conflict` | duplicate `(owner, source, preset, param, fire_time)` | create |
| `source_disabled` | the schedule's source is not enabled | `kb_schedule_run` (nothing to run) |
| `run_failed` | `run_pipeline` ran and returned `failed` | `kb_schedule_run` (run audited as `failed`) |
| `internal_error` | unexpected | any |
| `not_implemented_yet` | BR-10 stub | the four stubs |

`schedule_not_found` is deliberately used for *both* "doesn't exist" and "you
can't see it" (owner-scoped) so the response does not leak the existence of
another user's schedule (cross-user isolation).

## 6. Transport

The MCP server is transport-agnostic (004 FR-5 / BR-10.6): **stdio** and
**HTTP/SSE** expose the identical tool set and identical audit. A community
user points **any** MCP client (Claude Desktop, Cursor, pi, DSH, Hermes, or a
bespoke client) at their local `kb-mcp` with a token:

- **stdio** — the agent spawns the `kb-mcp` process as a child; MCP frames
  flow over the process's stdin/stdout. No network, no URL; the token is
  passed out-of-band (e.g. an env var the spawned process reads).
- **HTTP/SSE** — the agent points at a running `kb-mcp` endpoint (a
  transport URL the owner has provisioned); the token is sent as a `Bearer`
  credential.

Either way the agent receives the same ten tools, the same success/error
envelope, the same role-gate, and the same audit. The chosen MCP library /
transport plumbing is a 004 implementation detail; the contract (tool names,
schemas, audit shape, parity) is fixed.
