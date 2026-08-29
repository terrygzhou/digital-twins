# Feature Specification: MCP Scheduler Tools for Any Agent

**Feature Branch**: `004-mcp-scheduler-tools`

**Created**: 2026-08-29

**Status**: Draft (specify stage — clarify/plan/tasks pending)

**Input**: User description: "BRD slice BR-11.5 (requirement.md §2): extend the MCP server (BR-10) with scheduler tools so any MCP-capable agent can manage schedules and trigger runs — kb_schedule_list/create/update/delete, kb_schedule_run, kb_run_history; caller-scoped by default with a reserved per-schedule ACL (Q9 placeholder); transport-agnostic; identical audit with trigger=mcp + agent_kind."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Agents manage schedules over MCP (Priority: P1)

Any MCP-capable agent (Hermes, Claude Desktop, Cursor, pi, DSH, …) with a valid token can list schedules (with next fire time) and create/update/delete them (admin or scheduler role), plus trigger a one-shot run now and fetch run history for a user/source/time range (BR-11.5.1).

**Why this priority**: this is the slice's reason to exist — scheduling reachable from any agent, not just the host's CLI/UI.

**Independent Test**: An external MCP client calls each of the six tools; each produces the expected state change and audit record.

**Acceptance Scenarios**:

1. **Given** an authenticated scheduler-role user, **When** they call `kb_schedule_create`/`update`/`delete`, **Then** the 002 schedule store reflects it and each call is audited.
2. **Given** a schedule, **When** `kb_schedule_run` is called, **Then** a one-shot run executes through 002's shared pipeline (one-record-not-N holds vs. all other paths).
3. **Given** runs over a range, **When** `kb_run_history` is called with user/source/time-range, **Then** the matching audit records are returned in the 001/002 record shape.
4. **Given** a reader-role user, **When** they call any mutating scheduler tool, **Then** 403-equivalent tool error (003 role model applies).

### User Story 2 - Caller-scoped by default, ACL reserved (Priority: P1)

In v1 any authenticated user may call the scheduler tools, but the tools operate on the caller's *own* schedules and run history by default — a user cannot list or trigger another user's schedules without admin/scheduler privilege (Q9). The 002 `schedule.acl` field (default `owner`) is the reserved extension point for per-schedule ACLs, which are a follow-up; the schema must support it later without migration (BR-11.5.5).

**Why this priority**: the access-control placeholder is the security decision of this slice.

**Independent Test**: Bob cannot see or trigger Alice's schedule; admin can; the `acl` column exists with default `'owner'` and is accepted by create/update.

**Acceptance Scenarios**:

1. **Given** bob (reader) and alice's schedule, **When** bob calls `kb_schedule_list`/`kb_schedule_run`, **Then** alice's schedule is invisible/untriggerable to him.
2. **Given** an admin, **When** they list all schedules, **Then** all users' schedules appear (documented).
3. **Given** the 002 schema, **When** this slice ships, **Then** `schedule.acl` is present (default `owner`) and tool code enforces owner-scoping through a single check function that a future ACL can replace.

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

- FR-1 (BR-11.5.1): six tools — `kb_schedule_list`, `kb_schedule_create`, `kb_schedule_update`, `kb_schedule_delete`, `kb_schedule_run`, `kb_run_history` — with role gating per 003.
- FR-2 (BR-11.5.5/Q9): caller-scoped default (own schedules/history only), admin sees all; `schedule.acl` reserved (default `owner`), enforcement through one swappable check; granular ACL = follow-up.
- FR-3 (BR-11.5.2): token-only onboarding; `agent-guides.md` canonical and updated.
- FR-4 (BR-11.5.3): audit parity — `trigger='mcp'`, `agent_kind`, identical record shape.
- FR-5 (BR-11.5.4): stdio + HTTP/SSE transports expose the identical tool set.

### Key Entities

- **MCP tool** — name, args, error shape (stable contract for agents).
- **schedule.acl** — reserved column, v1 value `owner`.
- **agent_kind** — free-text provenance (existing `kb_ingest` pattern).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- SC-001: All six tools pass a contract test per transport (args, success shape, error shape).
- SC-002: Cross-user isolation: zero leakage of other users' schedules/history in the default scope — automated check.
- SC-003: MCP-triggered run dedups identically to CLI/served runs (top acceptance check extended to the mcp path) — automated check.
- SC-004: Every mutating MCP call produces exactly one audit row with `trigger='mcp'` + `agent_kind` — automated check.
- SC-005: 001/003 guards (portability, roles) stay green.

## Assumptions

- A1: Depends on 002 (schedule store + shared pipeline) and 003 (accounts, personal tokens, roles). Sequenced after both.
- A2: The BR-10 MCP server (external baseline) already provides search/chat/ingest/health; this slice extends its tool registry, not its transport plumbing (parity is asserted, not rebuilt — corrected in plan if BR-10's transport differs).
- A3: `agent_kind` values: the client's declared kind or `unknown` when undeclared (documented).
- A4: Tool errors use the MCP error convention with a machine-readable `code` (e.g. `permission_denied`, `schedule_not_found`) so agents can branch on them.

## Clarifications

All access-control decisions are locked by Q9 (placeholder, owner-scoped v1, `acl` reserved). Open for clarify: (c1) exact tool argument/return schemas (candidate: mirror `contracts/scheduler.md` field names); (c2) whether `kb_run_history` for admin on *other* users' runs is audit-logged as a distinct event (recommendation: yes, `trigger='mcp'`, `scheduled_by=<actor>`, target user in the record).
