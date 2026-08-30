# Feature Specification: Multi-User Sign-Up, Roles & Per-User Surfaces

**Feature Branch**: `003-multi-user`

**Created**: 2026-08-29

**Status**: Complete (v0.3.0, all 24 tasks done)

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

### Clarify-stage resolutions (2026-08-29)

**C-1 — Personal token schema.** Key Entity says "personal token (or token table)" — this is a genuine fork. Resolution: a separate `personal_tokens` table (columns: `id`, `account_email` FK, `token_hash` TEXT UNIQUE, `created_at`, `last_used_at`, `revoked INTEGER NOT NULL DEFAULT 0`) is preferred over a column on `accounts`. Rationale: (a) a user may hold multiple active tokens (browser session + API key + MCP token), (b) revocation of one token must not affect others (US3 S3: "revoking one does not affect the other"), and (c) the table keeps the `accounts` table shape unchanged so 001's `authenticate()` continues to work without modification. The shared service token (BR-10) remains a separate, static credential checked before personal-token lookup; personal tokens are additive, not a replacement.

**C-2 — Role enforcement surface.** The spec says "enforced everywhere" (US2) and "403 with a named reason" (US2 S1) but does not state *where* the check happens. 002's `StatusServer` has no auth middleware; `run_pipeline` is a pure function with no role parameter. Resolution: role checks live at the HTTP/API route layer (a thin middleware or per-route guard) for all mutating endpoints (schedule CRUD, run trigger, account management, config write). The pipeline (`run_pipeline`) does NOT check roles — it trusts the caller to have already passed the gate. The CLI's `run --as` (002) is an owner label (R-12), not an auth check; 003 extends it: after authentication succeeds (password or personal token), the CLI verifies the caller's role permits the action before writing audit rows. The web UI's sign-in flow returns a session token (short-lived, server-side session store or signed cookie); subsequent requests carry that token and the same middleware gates them.

**C-3 — Per-user config overrides: storage and precedence.** US3 says "alice's hermes cap=50" but 001's config layer is a single global dict resolved from env → kb.local.yml → kb.yml → defaults. Resolution: per-user overrides are stored in a new `user_config` table (columns: `account_email` FK, `source` TEXT, `key` TEXT, `value` TEXT, `updated_at`, UNIQUE(account_email, source, key)) — a key-value store for per-user, per-source knob overrides. Precedence for a scheduled run under user U: `user_config(U, source, key)` > global config resolution. The pipeline receives a *merged* config dict (global config with per-user overrides applied for the scheduled owner) so `run_pipeline` itself is unchanged — the merge happens at the caller (serve tick or CLI `run --as`). This keeps the pipeline a pure function of its arguments (constitution II) and avoids adding a role/user parameter to `run_pipeline`'s signature.

**C-4 — First-account creation: `init` vs. `/signup`.** US1 says "via `init` or `/signup`" but 001's `init` command creates endpoints + state DB + starter config, not an account. 002 added `accounts` table + `authenticate()` but no account-creation path. Resolution: 003 adds account creation to the `init` flow: after the state DB is migrated, `init` prompts for an email + password (or reads from env vars `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD`) and creates the first account as admin. The web UI's `/signup` endpoint (added in 003) creates subsequent accounts as readers. Both paths share the same account-creation helper (role determination: "first row in accounts?" → admin, else reader). This is not a re-litigation of Q8 (simple email+password, no OAuth/verification); it resolves *where* the first account is created.

**C-5 — `/status` auth gating (002 A3 ceiling).** US4 S3 says 002's `/status` "is auth-gated by the same token/session model." 002's `StatusServer` takes `(addr, db, config)` — no auth parameter. Resolution: 003 adds an optional `auth_checker` callable to `StatusServer.__init__` (signature: `auth_checker(request_headers) -> bool | str`; returns True for allow, a string for 403/401 reason, or False for 401). The serve CLI passes a checker that accepts (a) the shared service token (BR-10) or (b) a valid personal token; requests without either get 401. The `/status` handler wraps its `do_GET` with this check before returning the payload. This is a backward-compatible extension: existing callers that don't pass `auth_checker` get the 002 behavior (no auth) unchanged.
