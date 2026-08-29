# Feature Specification: Multi-User Sign-Up, Roles & Per-User Surfaces

**Feature Branch**: `003-multi-user`

**Created**: 2026-08-29

**Status**: Draft (specify stage — clarify/plan/tasks pending)

**Input**: User description: "BRD slice BR-11.4 (requirement.md §2): multi-user sign-up & sign-in — simple email+password accounts (Q8), admin/scheduler/reader roles with last-admin guard (Q3), per-user config overrides + run history + personal tokens, v1 soft data isolation via owner tags (Q2: hard isolation is follow-up), sign-in on web UI / API / MCP / CLI."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Simple sign-up (Priority: P1)

A user creates an account with their email as the username and a password — no OAuth, no email verification, no admin gating (Q8). On a fresh install the first account becomes admin; subsequent sign-ups are reader by default (BR-11.4.1, BR-11.4.6).

**Why this priority**: nothing else in this slice is reachable without accounts.

**Independent Test**: On a fresh install, create two accounts; the first has role `admin`, the second `reader`.

**Acceptance Scenarios**:

1. **Given** a fresh install, **When** the first account is created (via `init` or `/signup`), **Then** it has role `admin`.
2. **Given** an existing account, **When** a second user signs up, **Then** it has role `reader` and is fully usable for query.
3. **Given** a duplicate email, **When** sign-up is attempted, **Then** it fails fast with a clear error and no second row.

### User Story 2 - Three roles, enforced everywhere (Priority: P1)

Roles: `admin` (all surfaces), `scheduler` (manage schedules + trigger runs + own run history; MUST NOT reconfigure endpoints, manage accounts, or edit global config), `reader` (query only; mutating routes denied with 403) — extending BR-9.5. The last remaining admin MUST NOT be deleted or demoted. Role assignment is an admin action.

**Why this priority**: Q3 locked this exact set; enforcement is the security boundary of the whole multi-user slice.

**Independent Test**: A reader's mutating call gets 403; a scheduler can create a schedule but not change the Qdrant endpoint; the last admin cannot be demoted or deleted.

**Acceptance Scenarios**:

1. **Given** a reader, **When** any mutating route is called, **Then** the response is 403 with a named reason.
2. **Given** a scheduler, **When** they call schedule CRUD / run trigger / own history, **Then** all succeed; when they call endpoint config, account management, or global channel config, **Then** 403.
3. **Given** one remaining admin, **When** deletion or demotion is attempted, **Then** it is refused with a clear error.
4. **Given** an admin, **When** they promote a reader to `scheduler`, **Then** the change applies immediately and is recorded in the audit trail.

### User Story 3 - Per-user workspace (Priority: P2)

Each user has: their own channel/source config overrides (which sources they schedule, per-source caps, their tags) stored without affecting other users' runs; their own run history (audit records filtered to runs they triggered or that ran under their account); their own API/MCP credentials (a personal token in addition to the shared service token) (BR-11.4.2).

**Why this priority**: makes multi-user more than multi-login — the "my KB" semantics.

**Independent Test**: Alice sets her hermes cap to 50 and Bob's run is unaffected; each user's run history shows only their runs; each holds a distinct personal token that authenticates independently.

**Acceptance Scenarios**:

1. **Given** alice's override `max_items: 50` on hermes, **When** bob's hermes schedule fires, **Then** bob's configured cap applies (not alice's).
2. **Given** mixed runs, **When** each user views their history, **Then** only runs under their account appear.
3. **Given** two users, **When** each authenticates with their personal token, **Then** both succeed independently; revoking one does not affect the other.

### User Story 4 - Sign-in on every surface (Priority: P2)

Credentials (and personal tokens) are accepted on (a) the web UI, (b) the HTTP API, (c) the MCP server (BR-10.5 session or token), and (d) the scheduler CLI (`run --as <user>`, already 002). Unauthenticated access to any surface is denied (BR-11.4.5, BR-9.2).

**Why this priority**: one identity model across surfaces; 002's `--as` already plugs into it.

**Independent Test**: The same credentials authenticate on all four surfaces; each surface without credentials is denied.

**Acceptance Scenarios**:

1. **Given** valid credentials, **When** signing in on UI/API/MCP/CLI, **Then** each surface accepts them and attributes the session to the user.
2. **Given** no/invalid credentials, **When** any protected surface is called, **Then** it is denied (401/403) and nothing executes.
3. **Given** 002's `/status` endpoint, **When** 003 lands, **Then** it is auth-gated by the same token/session model (002's A3 ceiling is closed).

### User Story 5 - v1 data isolation: soft, tag-based (Priority: P3)

All users share the same KB store (single Qdrant collection + Neo4j graph). Per-user tags are stamped on ingested points so a user may query "only what I ingested" (post-filter on `owner == <user>` / `tag == <user>-ingest`). Hard per-user isolation is explicitly a follow-up (Q2) (BR-11.4.3).

**Why this priority**: completes the v1 isolation story without a second architecture.

**Independent Test**: Points ingested under alice carry her owner tag; an owner-filtered query returns only her content.

**Acceptance Scenarios**:

1. **Given** runs under two users, **When** points are queried with an owner filter, **Then** each user sees only their ingested content.
2. **Given** a shared (system) run, **When** any user queries, **Then** system content is visible per the documented sharing default.

## Requirements *(mandatory)*

### Functional Requirements

- FR-1 (BR-11.4.1): sign-up with email-as-username + password; open on fresh install; no OAuth/verification/admin-gating in v1 (all three are follow-up).
- FR-2 (BR-11.4.6/BR-9.1): first account = admin; later sign-ups = reader.
- FR-3 (BR-11.4.4/BR-9.5): admin/scheduler/reader roles with the exact capability matrix above; last-admin guard; role changes are admin actions.
- FR-4 (BR-11.4.2): per-user source overrides, per-user run history view, personal tokens coexisting with the shared service token.
- FR-5 (BR-11.4.5): sign-in accepted on web UI, HTTP API, MCP, CLI; unauthenticated access denied everywhere.
- FR-6 (BR-11.4.3): shared store + per-user owner tags + owner-filtered query; hard isolation = follow-up, stated in docs.

### Key Entities

- **Account** — 001 row extended: personal token (or token table), created/last-active timestamps.
- **UserConfig** — per-user source overrides (caps, enabled set, tags).
- **Tag/owner stamp** — on 001 IngestItem/Point payloads.
- **AuditRun** — `scheduled_by` now always a real account (or `system`).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- SC-001: Role capability matrix — 100% of matrix rows covered by automated tests (reader/scheduler/admin × mutating/query).
- SC-002: Last-admin deletion/demotion is refused — automated check.
- SC-003: Per-user override isolation: one user's cap change never alters another user's run — automated check.
- SC-004: All four sign-in surfaces accept the same credentials; all deny unauthenticated — automated check per surface.
- SC-005: Owner-tagged points are retrievable by owner filter — automated check.
- SC-006: 001 portability + one-record guards stay green (no host paths, no duplicate points).

## Assumptions

- A1: 002's scheduler is the only mutating ingestion driver; 003 does not add new trigger paths (MCP runs route through 002's shared pipeline).
- A2: The web UI is minimal in v1: sign-in, schedule view/edit, run history — the 002 status payload feeds it; no SPA framework decision in this spec (research resolves it).
- A3: Password hashing follows 001's `password_hash` column (hash-algorithm choice deferred to plan/research; must be stdlib-compatible, e.g. PBKDF2 via `hashlib`).
- A4: The shared service token (BR-10) is unchanged; personal tokens are additive.
- A5: Run-history "mine" = runs where `scheduled_by` is the user's account or that user triggered manually/MCP.

## Clarifications

All role and isolation decisions are locked by Q2/Q3/Q8 in requirement.md §5 — not open. Boundary note: the web UI ships here (BR-11.4.5a) but is deliberately minimal (A2); a richer dashboard is a follow-up.
