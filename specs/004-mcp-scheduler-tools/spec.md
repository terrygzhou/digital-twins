# Feature Specification: MCP Scheduler Tools for Any Agent

**Feature Branch**: `004-mcp-scheduler-tools`

**Created**: 2026-08-29

**Status**: Complete (all 32 tasks done; BR-10 stubs superseded by 007-kb-search-chat)

**Input**: User description: "BRD slice BR-11.5 (requirement.md §2): extend the MCP server (BR-10) with scheduler tools so any MCP-capable agent can manage schedules and trigger runs — kb_schedule_list/create/update/delete, kb_schedule_run, kb_run_history; caller-scoped by default with a reserved per-schedule ACL (Q9 placeholder); transport-agnostic; identical audit with trigger=mcp + agent_kind."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Agents manage schedules over MCP (Priority: P1)

Any MCP-capable agent (Hermes, Claude Desktop, Cursor, pi, DSH, …) with a valid token can list schedules (with next fire time) and create/update/delete them (admin or scheduler role), plus trigger a one-shot run now and fetch run history for a user/source/time range (BR-11.5.1).

**Why this priority**: this is the slice's reason to exist — scheduling reachable from any agent, not just the host's CLI/UI.

**Independent Test**: An external MCP client calls each of the six tools; each produces the expected state change and audit record.

**Acceptance Scenarios**:

1. **Given** an authenticated scheduler-role user, **When** they call `kb_schedule_create`/`update`/`delete`, **Then** the 002 schedule store reflects it and each call is audited.
2. **Given** a schedule, **When** `kb_schedule_run` is called, **Then** a one-shot run executes through 002's shared pipeline (one-record-not-N holds vs. all other paths).
3. **Given** runs over a range, **When** `kb_run_history` is called with user/source/time-range, **Then** the matching audit records are returned in the 001/002 record shape (`run_id`/`started_at`/`completed_at`/`status`/`trigger`/`scheduled_by`/`per_source_counts`).
4. **Given** a reader-role user, **When** they call any mutating scheduler tool (`kb_schedule_create`/`update`/`delete`/`kb_schedule_run`), **Then** the tool error has machine-readable `code=permission_denied` naming the missing capability (003 R3 role model applies; a reader *can* still `kb_schedule_list` own + `kb_run_history` own).

### User Story 2 - Caller-scoped by default, ACL reserved (Priority: P1)

In v1 any authenticated user may call the scheduler tools, but the tools operate on the caller's *own* schedules and run history by default — a user cannot list or trigger another user's schedules without admin/scheduler privilege (Q9). The 002 `schedule.acl` field (default `owner`) is the reserved extension point for per-schedule ACLs, which are a follow-up; the schema must support it later without migration (BR-11.5.5).

**Why this priority**: the access-control placeholder is the security decision of this slice.

**Independent Test**: Bob cannot see or trigger Alice's schedule; admin can; the `acl` column exists with default `'owner'` and is accepted by create/update.

**Acceptance Scenarios**:

1. **Given** bob (reader or scheduler) and alice's schedule, **When** bob calls `kb_schedule_list`/`kb_schedule_run` with no admin privilege, **Then** alice's schedule is invisible/untriggerable to him (default scope = caller's own).
2. **Given** an admin, **When** they list all schedules, **Then** all users' schedules appear (documented).
3. **Given** the shipped 002 schema, **When** 004 is exercised, **Then** `schedule.acl` is present (default `owner`, already in the v2 DDL), is accepted by create/update, and tool code enforces owner-scoping through a single swappable check function (`can_access_schedule`) that a future ACL can replace without touching the tool bodies.

### User Story 3 - One-line agent onboarding (Priority: P2)

The generic agent onboarding path is a single documented line: an agent with a valid token may `kb_search`, `kb_chat`, and — if admin — `kb_ingest` and the new scheduler tools, with no host-specific setup beyond the token (BR-11.5.2). `docs/references/agent-guides.md` (existing) is the canonical onboarding doc and gains the new tools.

**Why this priority**: "any agent" is the BR's title promise.

**Independent Test**: A fresh MCP client configured with only the token + transport URL gets the full tool list including the six scheduler tools; a non-admin client gets them but mutating calls are refused.

**Acceptance Scenarios**:

1. **Given** a new MCP client + token, **When** it lists tools, **Then** the scheduler tools are present and documented in `agent-guides.md`.
2. **Given** a non-admin token, **When** `kb_schedule_create` is called, **Then** the tool error names the missing role.

### User Story 4 - Transport-agnostic + identical audit (Priority: P2)

The MCP server is transport-agnostic (BR-10.6: stdio and HTTP/SSE); a community user pointing any MCP client at their local `kb-mcp` with a token gets the full tool set (BR-11.5.4). Every scheduler-tool invocation is audited identically to API/UI/CLI runs, with `trigger: "mcp"` and `agent_kind` recorded (BR-11.5.3, the existing `kb_ingest` pattern).

**Why this priority**: transport parity and audit parity are the invariants that keep "any agent" honest.

**Independent Test**: The same call over stdio and over HTTP/SSE yields identical state changes; the audit row shows `trigger='mcp'` + a populated `agent_kind`.

**Acceptance Scenarios**:

1. **Given** both transports, **When** `kb_run_history` is called on each, **Then** identical results.
2. **Given** a `kb_schedule_run` over MCP, **When** the run completes, **Then** exactly one audit row exists with `trigger='mcp'`, `agent_kind` set, and the 001/002 record fields complete.

## Requirements *(mandatory)*

### Functional Requirements

- FR-1 (BR-11.5.1): six tools — `kb_schedule_list`, `kb_schedule_create`, `kb_schedule_update`, `kb_schedule_delete`, `kb_schedule_run`, `kb_run_history` — **role-gated per 003 R3** (locked, ruling R4): `kb_schedule_create`/`update`/`delete` require `schedule_crud`; `kb_schedule_run` requires `trigger_run`; `kb_run_history` on the caller's own runs requires `view_own_history`, admin viewing *all* users' runs requires `view_all_history`; `kb_schedule_list` on the caller's own schedules requires `query_status`. A `reader` (caps `sign_in`/`query_status`/`view_own_history`) may list its own schedules and view its own history but is **denied** every mutating tool with a machine-readable `permission_denied` error naming the missing capability. Gating reuses 003's `digital_twins.accounts.require_capability(role, capability, action_label)`.
- FR-2 (BR-11.5.5/Q9, rulings R2/R5): v1 tools operate on the **caller's own** schedules/history by default; an admin may list/trigger/query *all* users. The `schedule.acl` column (already present in the shipped 002 schema, default `'owner'`) is accepted by create/update; granular per-schedule ACL enforcement is a **documented follow-up**. Access decisions route through a single swappable check function (e.g. `can_access_schedule(schedule_row, caller_email, caller_role) -> bool`) so a future ACL can replace it without touching the tool bodies.
- FR-3 (BR-11.5.2): token-only onboarding; `agent-guides.md` canonical and updated to list the six scheduler tools + their role requirements.
- FR-4 (BR-11.5.3, rulings R3/R6): audit parity — every scheduler-tool invocation writes exactly one `audit_runs` row with `trigger='mcp'`, `scheduled_by=<caller account>`, `agent_kind` = the client's declared kind or `unknown` (free-text provenance), and the 001/002 record shape (`run_id`/`started_at`/`completed_at`/`status`/`per_source_counts`). Admin viewing *another* user's run history is a distinct audited event: `trigger='mcp'`, `scheduled_by=<actor>`, target user recorded in the row (ruling R3).
- FR-5 (BR-11.5.4, ruling R7): stdio and HTTP/SSE transports expose the **identical tool set and identical audit** (parity asserted by contract test; the chosen MCP library/transport is a PLAN-stage decision in research.md, not a spec decision).
- FR-6 (ruling R2): tool argument/return schemas **mirror the 003 `contracts/scheduler.md` field names exactly** — `schedule_id`, `source`, `preset`, `param`, `fire_time`, `acl` for schedule objects; `run_id`/`started_at`/`completed_at`/`status`/`trigger`/`scheduled_by`/`per_source_counts` for audit records. This is a stable agent-facing contract.

### Key Entities

- **MCP tool** — name, args, error shape (stable contract for agents; field names mirror 003 `contracts/scheduler.md`, ruling R2).
- **schedule.acl** — reserved column (shipped in the 002 schema, default `'owner'`), accepted by create/update, granular enforcement a follow-up (ruling R5).
- **can_access_schedule** — the single swappable owner-scoping check (ruling R5); v1 returns true iff the caller owns the schedule or is admin.
- **agent_kind** — free-text provenance; value = the client's declared kind or `unknown` (ruling R6).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- SC-001: All six tools pass a contract test per transport (args, success shape, error shape).
- SC-002: Cross-user isolation: zero leakage of other users' schedules/history in the default scope — automated check.
- SC-003: MCP-triggered run dedups identically to CLI/served runs (top acceptance check extended to the mcp path) — automated check.
- SC-004: Every mutating MCP call produces exactly one audit row with `trigger='mcp'` + `agent_kind` — automated check.
- SC-005: 001/003 guards (portability, roles) stay green.
- SC-006: An admin calling `kb_run_history` for *another* user produces a distinct audit row with `trigger='mcp'`, `scheduled_by=<admin actor>`, and the target user recorded in the row (ruling R3) — automated check.

## Assumptions

- A1: Depends on 002 (schedule store + shared pipeline) and 003 (accounts, personal tokens, roles). Sequenced after both.
- A2 (corrected, ruling R1): There is **no MCP server in this repository** — 001/002/003 built none; the BR-10 baseline is external and NOT present here. 004 therefore **establishes the MCP server from scratch**: the tool registry, the stdio + HTTP/SSE transports, the BR-10.5 token auth (reusing 003's `auth_checker` / `personal_tokens.verify` / `sessions.verify` / `verify_personal_token` / `verify_session`), and the six scheduler tools. The BR-10 `kb_search` / `kb_chat` / `kb_ingest` / `kb_health` tools are **out of scope for 004** (004's scope = the scheduler tools + the server scaffolding that carries them); they may be registered-but-stubbed or noted as a follow-up.
- A3: `agent_kind` values: the client's declared kind or `unknown` when undeclared (documented).
- A4: Tool errors use the MCP error convention with a machine-readable `code` (e.g. `permission_denied`, `schedule_not_found`) so agents can branch on them.

## Clarifications

All access-control decisions are locked by Q9 (placeholder, owner-scoped v1, `acl` reserved) and 003's R3 role matrix. The two open clarify items (c1, c2) plus the seven locked rulings (R1–R7) are resolved below.

### Session 2026-08-29 (clarify stage)

- **Q (c1) → A (locked, ruling R2):** Tool argument/return schemas mirror `specs/003-multi-user/contracts/scheduler.md` field names **exactly**. Schedule objects carry `schedule_id`, `source`, `preset`, `param`, `fire_time`, `acl` (plus `owner`/`enabled`/`next_fire_at`/`created_at`/`updated_at` as read fields). Audit records carry `run_id`, `started_at`, `completed_at`, `status`, `trigger`, `scheduled_by`, `per_source_counts`. `schedule_id` is the 002 `schedules.id` INTEGER PK surfaced to agents. Error objects use the A4 machine-readable `code` (e.g. `permission_denied`, `schedule_not_found`, `invalid_preset`). This is a stable agent-facing contract; a future ACL (R5 follow-up) must not rename these fields.

- **Q (c2) → A (locked, ruling R3):** Admin viewing *another* user's run history via `kb_run_history` **is** audit-logged as a distinct event: `trigger='mcp'`, `scheduled_by=<admin actor account>`, target user recorded in the audit row (a new read-only query event; it does NOT mutate any schedule/run). This closes the "who looked at whose history" gap (constitution V: auditability) without altering the one-record-not-N invariant (a read is not an ingestion). A caller's own-history query does **not** write such a row (it is the normal `list_runs(user=...)` view, not a cross-user access event).

- **Q (R1, scope of the MCP server) → A (locked):** There is **no MCP server in this repository** (001/002/003 built none; the BR-10 baseline is external and absent). 004 builds the **full** MCP server from scratch — tool registry, stdio + HTTP/SSE transports, BR-10.5 token auth (reusing 003's `auth_checker` / `personal_tokens.verify` / `sessions.verify` / `verify_personal_token` / `verify_session`), and the six scheduler tools. The BR-10 `kb_search`/`kb_chat`/`kb_ingest`/`kb_health` tools are **out of scope** for 004 (004 = scheduler tools + the server scaffolding that carries them); they may be registered-but-stubbed or noted as a follow-up.

- **Q (R2, transport library) → A (locked, deferred to plan):** The stdio + HTTP/SSE transports must expose the **identical tool set and identical audit** (ruling R7). The specific MCP library / transport implementation is a **PLAN-stage decision** (research.md), not a spec decision — the spec fixes the *contract* (tool names, schemas, audit shape, parity), not the transport plumbing.

- **Q (R4, role-gating mapping) → A (locked):** Per 003 R3 + `digital_twins.accounts.ROLE_CAPS`: `kb_schedule_create`/`update`/`delete` require `schedule_crud`; `kb_schedule_run` requires `trigger_run`; `kb_run_history` on the caller's own runs requires `view_own_history`, on *all* users' runs requires `view_all_history`; `kb_schedule_list` on the caller's own schedules requires `query_status`. A `reader` (caps `sign_in`/`query_status`/`view_own_history`) may list own + view own history but is denied every mutating tool with `code=permission_denied` naming the missing capability. Enforcement reuses 003's `require_capability(role, capability, action_label)`.

- **Q (R5, caller-scoped default / ACL) → A (locked, per Q9):** v1 tools operate on the **caller's own** schedules/history by default; admin sees all. `schedule.acl` is **already shipped** in the 002 schema (`acl TEXT NOT NULL DEFAULT 'owner'`, see `digital_twins/state/models.py` DDL_V2) — 004 *consumes* it (accepts it in create/update) rather than adding it. Granular ACL enforcement is a documented follow-up; enforcement routes through a single swappable check `can_access_schedule(schedule_row, caller_email, caller_role) -> bool` so a future ACL can replace it without touching the tool bodies.

- **Q (R6, agent_kind) → A (locked):** `agent_kind` is free-text provenance; value = the client's declared kind or `unknown` when undeclared (documented). Recorded on every `trigger='mcp'` audit row (FR-4).

- **Q (R7, transport parity) → A (locked):** stdio + HTTP/SSE expose the identical tool set + identical audit (see R2 bullet).

- **Q (R8, admin cross-user history read — audit row shape) → A (locked):** The admin cross-user read event (c2/R3) writes one `audit_runs` row with `status='ok'` (the CHECK constraint is `('ok','partial','failed')`; a read that returns is `'ok'`), a **fresh `run_id` UUID v4 per read** (a read is not an ingestion, so it does not collide with any run's `run_id`), `trigger='mcp'`, `scheduled_by=<admin actor account>`, and the target user recorded inside the `per_source_counts` JSON column (e.g. `{"mcp_history_query": {"target_user": "<email>", "agent_kind": "<kind>"}}`). A caller's own-history query writes **no** such row (the normal `list_runs(user=...)` view). One read = one row; repeated reads = repeated rows (documented; it is an access log, not an ingestion log).

- **Q (R9, admin "list all schedules" enforcement) → A (locked):** Admin's ability to list *all* schedules rides on the `admin` role directly — 003's `ROLE_CAPS` has **no** `view_all_schedules` capability and this slice does **not** add one (the R3 matrix stays frozen at 11 caps). Enforcement: `can_access_schedule(schedule_row, caller_email, caller_role)` returns True for the schedule's owner, and for any schedule when `caller_role == 'admin'`; otherwise False. `kb_schedule_list` with the all-users flag additionally requires `view_all_history` is NOT the gate — the gate is the admin-role check in `can_access_schedule` (documented). A future ACL (R5 follow-up) replaces `can_access_schedule`'s body, not its call sites.

- **Q (R10, BR-10 tool stubs) → A (locked):** The four out-of-scope BR-10 tools (`kb_search`, `kb_chat`, `kb_ingest`, `kb_health`) are **registered but stubbed** in v1 — present in the tool list (so a fresh client sees a complete registry) but each returns a machine-readable error `code=not_implemented_yet` naming the follow-up slice, rather than being omitted. They are documented as follow-ups in `docs/references/agent-guides.md`. This keeps the tool list honest and stable while 004's scope stays the six scheduler tools.

- **Q (R11, plan-stage: schedule CRUD audit semantics) → A (locked):** "Every mutating MCP call writes one `trigger='mcp'` audit row" is refined to: only **`kb_schedule_run`** (a real ingestion run) and the **admin cross-user `kb_run_history` read** (R8 access-log row) write `audit_runs` rows. Schedule **CRUD** (`kb_schedule_create`/`update`/`delete`) writes **NO** `audit_runs` row (it is a state mutation, not a run; logged via structured logging per constitution V, not an ingestion audit). Rationale: `audit_runs` is a *run* log (status domain `ok/partial/failed`); a CRUD row would pollute `kb_run_history` (which reads `audit_runs`). This preserves the one-record-not-N invariant and keeps the run-log clean.

- **Q (R12, plan-stage: new config knobs) → A (locked):** 004 introduces two new knobs: `mcp.port` (default `8770`) and `mcp.service_account_email` (default `"system"`). Both are additive to `kb.yml`/`.env.example` and MUST be documented per constitution IV (the tasks stage adds them to the knob docs + the standing `test_knob_docs.py` guard stays green).

- **Q (R13, plan-stage: access-log row exclusion in history) → A (locked):** The R8 admin cross-user read row uses a synthetic `run_id` + `status='ok'` + `per_source_counts` containing the key `mcp_history_query`. The `kb_run_history` read path MUST exclude rows whose `per_source_counts` contains the `mcp_history_query` key (an access-log marker) from the "runs" view, so the synthetic row is never mistaken for an ingestion run. No schema change; documented filter in the read path.
