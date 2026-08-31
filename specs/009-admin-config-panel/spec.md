# Feature Specification: Web Admin — Service Configuration & Health Panel

**Feature Branch**: `009-admin-config-panel`

**Created**: 2026-08-31

**Status**: Complete (v0.9.0, all 18 tasks done; full suite 964/964)

**Input**: User description: "The web UI can/should set and test the endpoints of Qdrant and other dependent resources." 008 shipped the admin REST surface for service config (GET/POST `/api/config/services`, 008/US2 T023+T024) but the static UI never calls it, and no live connectivity test ("probe") exists anywhere on the web surface — the 4-service status table lives only in the CLI (`validate` / `health` via `health.preflight`).

## Clarifications

### Session 2026-08-31

- Q: Are credential fields (`qdrant.api_key`, `neo4j.user`/`neo4j.password`, `llm.api_key`, `embedding.api_key`) editable in the v1 panel, or is v1 URL-only with credentials via env/file/CLI? -> A: URL-only in v1 (recommended option accepted on the user's "proceed to tasks" directive). Credentials stay env/file/CLI; the panel shows only `*_set` booleans, preserving 008's secret-hygiene posture.
- Q: How does an admin clear a previously saved url/credential from `kb.local.yml` (fall back to env/defaults) - explicit clear semantics, or overwrite-only in v1? -> A: Overwrite-only in v1; the panel has no clear/delete control.

## User Scenarios & Testing

### User Story 1 — Fix a service endpoint from the web panel (Priority: P1)

An admin of a deployment with a misconfigured service (e.g. wrong Qdrant host) opens the web UI, finds the services panel, sees each service's effective endpoint and whether credentials are set (never the values), edits the URL, saves, and sees the change take effect — no CLI, no file editing, no restart.

**Why this priority**: This is the reported pain point (the user got a gate error in the UI with no way to fix the config from the UI). It reuses the existing 008 admin API (`POST /api/config/services` → atomic `merge_write` to `kb.local.yml`), so the delta is UI wiring, not new persistence semantics.

**Independent Test**: With the web server running: sign in as admin, change `qdrant.url` in the panel to a live host:port, save. `kb.local.yml` contains the new value (other keys preserved), the panel re-renders the new effective URL from the post-write view, and `digital-tokens validate` on the same host reports qdrant `ok`.

**Acceptance Scenarios**:

1. **Given** a running web server with qdrant unconfigured, **when** an admin opens the services panel, **then** the qdrant row shows an empty/unset URL, status `unconfigured`, and remediation text naming the exact knob.
2. **Given** an admin edits `qdrant.url` to `http://host:6333` and saves, **then** `kb.local.yml` is updated via the existing atomic path, and the panel refreshes from the post-write masked view (URL present, `api_key_set` unchanged).
3. **Given** an admin submits an invalid value (e.g. empty where required / wrong type), **then** the save is rejected with the existing 422 shape and the panel shows the error without discarding unsaved edits in other fields.
4. **Given** a service knob is currently shadowed by a `KB_*` env var, **then** that row displays an "env override in effect" indicator naming the exact variable (from `env_overrides`).

---

### User Story 2 — Test connectivity from the web panel (Priority: P1)

An admin clicks "Test" on a service row (or "Test all") and the panel shows live status — `ok | unconfigured | unreachable | auth-failed` — with per-service remediation, the same information the CLI `validate`/`health` renders, without leaving the browser.

**Why this priority**: Set-without-test is half the loop; testing connectivity is the user's explicit second expectation. It closes the set → verify loop entirely in the browser.

**Independent Test**: Point `qdrant.url` at a dead port, sign in as admin, click Test → the row shows `unreachable` + remediation. Fix the URL, Test again → `ok`. No CLI involved.

**Acceptance Scenarios**:

1. **Given** all four services configured and reachable, **when** the admin clicks "Test all", **then** all four rows show `ok` within the probe time budget.
2. **Given** the qdrant host is down, **when** the admin probes the qdrant row, **then** that row shows `unreachable` plus the exact remediation text (knob + env form) and the other rows are unaffected.
3. **Given** a service's credentials are rejected, **when** the admin probes, **then** the status is `auth-failed`, distinct from `unreachable`.
4. **Given** a service is unconfigured, **when** the admin probes, **then** the status is `unconfigured` with remediation.
5. **Given** a probe is in flight, **when** the admin triggers another probe, **then** the UI suppresses the duplicate request and shows no partial/stale state (single in-flight probe per service).

---

### User Story 3 — Non-admins see no configuration surface (Priority: P2)

A reader/editor session on the same deployment sees no services panel at all, and direct access to the configuration/probe routes is denied — endpoint topology and config metadata stay admin-only.

**Why this priority**: Security boundary. The existing API is already admin-gated, so this is UI hiding plus regression coverage of the gate on the new probe route.

**Independent Test**: Sign in as a reader: the panel element does not exist in the DOM. `GET`/`POST` the config routes and the probe route with a reader token → `403 permission_denied`.

**Acceptance Scenarios**:

1. **Given** a reader session, **when** the UI loads, **then** the services panel is not rendered.
2. **Given** a reader token, **when** the probe route (or `/api/config/services`) is called, **then** `403 permission_denied` in the existing shape.
3. **Given** an admin session, **when** the same calls are made, **then** `200` with the masked view / probe result.

---

### Edge Cases

- **Env override shadowing**: when a `KB_*` env var shadows a knob, the panel MUST make it visible (indicator + exact var name from `env_overrides`); a save to `kb.local.yml` succeeds but does not change the effective value until the env var is unset — the panel warns on that row.
- **Clearing a saved value**: Overwrite-only in v1 — the panel can set/overwrite URL values only; there is no clear/delete control. Falling back to env/defaults is done by editing `kb.local.yml` (or via CLI); the row's remediation text notes where the effective value comes from.
- **Unparseable existing `kb.local.yml`**: POST returns the existing 409 "existing kb.local.yml does not validate"; the panel surfaces it and performs no write.
- **Probe timeout bounds**: every probe path uses bounded socket timeouts; a hanging service must still yield `unreachable` within the budget, never "in progress forever".
- **Save during probe**: a save mid-probe must not corrupt `kb.local.yml` (existing atomic `merge_write`; probes read config only, write nothing).
- **Transient state only**: probe results are never persisted; nothing is written to the state DB by this feature.
- **Concurrent admins**: two admins saving concurrently — last write wins via the existing atomic `merge_write` (no new locking introduced).

## Requirements

### Functional Requirements

- **FR-001**: The web UI MUST render an admin-gated Services panel listing the four hard services (qdrant, neo4j, llm, embedding) with, per row: effective URL/endpoint value, credential-set indicators (booleans, never values), status pill, remediation text when not `ok`, and an env-override indicator.
- **FR-002**: The panel MUST let an admin edit URL fields and save via the existing `POST /api/config/services` (atomic `merge_write` to `kb.local.yml`); a successful save MUST refresh the panel from the post-write masked view.
- **FR-003**: The web surface MUST provide an admin-gated probe route returning, per service, `status ∈ {ok, unconfigured, unreachable, auth-failed}` + remediation, computed with the same health logic the CLI `validate`/`health` uses (`health.preflight` — no duplicated probe logic).
- **FR-004**: The panel MUST support per-service probe ("Test") and all-services probe ("Test all"), rendering the per-row status pills.
- **FR-005**: Every probe MUST be bounded by per-service socket timeouts such that "Test all" against four down/hanging services completes in ≤ 5 s wall-clock total.
- **FR-006**: Credential values MUST NEVER be returned by, echoed in, or logged by any new UI asset or route — the 008 FR-004 secret-hygiene guarantee extended to the new probe route and panel (covered by extending the existing secret-hygiene tests).
- **FR-007**: The Services panel MUST NOT render for non-admin sessions; the new probe route MUST return `403 permission_denied` (existing shape) for non-admin callers.
- **FR-008**: Invalid submissions MUST surface the existing 422 error shape in the panel without discarding unsaved edits in other fields.
- **FR-009**: The panel MUST display `env_overrides` (exact `KB_*` variable names) with a per-row indicator when a shadowing env var is present.
- **FR-010**: The v1 panel is URL-only: credential fields (`qdrant.api_key`, `neo4j.user`/`neo4j.password`, `llm.api_key`, `embedding.api_key`) are NOT editable in the panel — they are set via env / `kb.local.yml` / CLI. The panel displays only their `*_set` booleans (never the values).

### Key Entities

- **Service** (transient view object — no new table): name (`qdrant|neo4j|llm|embedding`), url-knob dotted path, credential knobs (name + `*_set` flag only), status, remediation, env-override flag.
- **ProbeResult** (transient response of the probe route): per-service `status` + `remediation`; never persisted.
- **Config service sections** (existing, 008): the `kb.local.yml` sections the panel writes through the existing `POST /api/config/services`.

No new persistent entities; no state-DB migration.

## Success Criteria

### Measurable Outcomes

- **SC-001**: An admin can repair a misconfigured service endpoint entirely from the web UI (view → edit → save → test → green status) in under 2 minutes, with no file access and no CLI.
- **SC-002**: "Test all" against four down/hanging services returns complete per-service results in ≤ 5 s (no request exceeds the probe budget).
- **SC-003**: Zero credential values in any web response body or server log across the new/changed routes (verified by extending the existing secret-hygiene unit tests to the probe route).
- **SC-004**: Non-admin sessions render zero services-panel DOM elements; direct route access yields `403` (regression-tested for both the existing config routes and the new probe route).
- **SC-005**: Full test suite green, including the standing portability (T006) and knob-docs (T027) guards; no host-specific paths, usernames, or install locations in shipped code, config defaults, or docs.

## Assumptions

- The panel extends the existing single-page static UI (`web/static/index.html` + `style.css`, no JS framework — consistent with 006), reusing the existing token/session auth handling.
- Save/view semantics are exactly the existing 008 admin API (GET/POST `/api/config/services`, error shapes 401/403/404/422/409, atomic `merge_write`); this feature adds the probe route and the UI — it does not rework config persistence.
- Probe = read-only liveness: the same per-service checks the CLI `validate`/`health` run (connect + auth where applicable). Probes write nothing (no test points, no collection creation).
- `chunking` knobs are out of scope for the v1 panel (the API accepts them; the panel need not expose them).
- Role semantics unchanged: admin is resolved via the existing accounts role mechanism (`accounts.get_role`, same as `/api/config/services`); the panel does not manage accounts/roles.
- Web server restart is not required for config changes to take effect on the next probe (config is re-read per request, consistent with 008's loader usage in the config handlers).
