# Feature Specification: Web Admin UI — Service Credential Fields (write-only)

**Feature Branch**: `011-config-credentials`

**Created**: 2026-08-31

**Status**: Draft (spec-only; supersedes 009 FR-010)

**Input**: Owner directive 2026-08-31: "ensure the password, api tokens can be set in the web UI. update the requirement." BR-12.2.1 already requires that service access credentials be settable from the web admin UI ("values persist to the machine-local config layer"). 009 deliberately shipped URL-only (its Clarifications session 2026-08-31 accepted "URL-only in v1"); this slice records that owner-directed reversal and delivers the remaining UI delta. No new API, knobs, or persistence semantics.

## Verified starting state (from shipped code, 0.9.0)

- **API already accepts credential writes.** `POST /api/config/services` (008/US2) schema-validates any subset of the four service sections + `chunking` and persists via `config.local_io.merge_write` → `kb.local.yml`. `qdrant.api_key`, `neo4j.user`, `neo4j.password`, `llm.api_key`, `embedding.api_key` are all schema-valid, persistable knobs.
- **GET remains masked.** `GET /api/config/services` returns only `*_set` booleans for credentials — never values (008 FR-004, 009 FR-006/SC-003).
- **UI gap.** The 009 panel (`web/static/index.html`) renders only the URL input per service row; credential `*_set` badges are display-only. `saveService()` posts only the URL knob.

## Clarifications

### Session 2026-08-31

- Q: Does the reversal also change the read side — should the panel display current credential values? -> A: No. Write-only: inputs are never pre-filled; GET keeps returning only `*_set` booleans. 008 secret-hygiene posture (values never in responses/logs) is preserved.
- Q: Is `neo4j.user` treated as a secret? -> A: It is not a secret, but it ships in the same credential pair and is edited through the same write-only control. It is the one field rendered as a plain (non-password) input, since its value is not confidential.
- Q: Can an admin clear a saved credential from the panel? -> A: No — inherits 009's overwrite-only decision (no clear/delete control in v1). A blank credential input on save omits the key from the POST body; it does NOT delete the saved value.

## User Scenarios & Testing

### User Story 1 — Complete external hosting from the panel (Priority: P1)

An admin whose deployment points at externally hosted Neo4j (or Qdrant/LLM) types the credential in the services panel, saves, and probes — the service reports `ok` (or at least no longer `auth-failed`) with no file edits, no CLI, no restart.

**Why this priority**: BR-12.2.1 names this the reason credentials are config-layer citizens; 008 shipped the API half, 009 shipped the URL half, and this slice closes the loop.

**Independent Test**: With the web server running: sign in as admin, enter `neo4j.password` in the panel, save. `kb.local.yml` contains the value; `GET /api/config/services` shows `password_set: true` (never the value); `POST /api/config/services/probe` for neo4j returns `status: ok` (or `unreachable`, not `auth-failed`).

**Acceptance Scenarios**:

1. **Given** an admin opens the services panel, **when** the panel renders, **then** each service row shows a write-only credential input (empty, never pre-filled) in addition to the URL input, with the existing `*_set` badge when a credential is already set.
2. **Given** the admin enters a value in `qdrant.api_key` and saves, **then** the POST body includes only the changed knobs, `kb.local.yml` gains `qdrant.api_key`, and the post-write view shows `api_key_set: true` without echoing the value.
3. **Given** the admin leaves a credential input blank while changing the URL, **when** they save, **then** the credential key is omitted from the write and the previously saved value is untouched.
4. **Given** a non-admin caller, **when** the panel is used, **then** nothing changes — the routes stay admin-gated exactly as in 008/009.

## Functional Requirements

- **FR-001**: The panel MUST render one write-only input per credential knob: `qdrant.api_key`, `neo4j.user`, `neo4j.password`, `llm.api_key`, `embedding.api_key` (neo4j's row carries both; the other three carry `api_key` only — no input may appear for a knob the service does not have).
- **FR-002**: Credential inputs MUST NOT be pre-filled from the GET response (which cannot carry values anyway) and MUST use `type="password"` — except `neo4j.user`, which is not a secret and uses a plain text input.
- **FR-003**: Save MUST include only non-empty credential inputs in the `POST /api/config/services` body; empty inputs are omitted (overwrite-only; no clear semantics).
- **FR-004**: No API changes: `GET /api/config/services`, `POST /api/config/services`, and `POST /api/config/services/probe` keep their 008/009 contracts, error shapes, and admin gating verbatim. No new config knobs; persistence remains `merge_write` → `kb.local.yml`.
- **FR-005**: Supersedes 009 FR-010: the panel is no longer URL-only; the credential fields above are editable in the panel, write-only.

## Success Criteria

- **SC-001**: After setting `neo4j.password` (and `neo4j.url`) from the panel against a live Neo4j, the in-panel Test button reports `ok` — a previously `auth-failed` service becomes reachable.
- **SC-002**: Zero credential values in any web response body or server log across the changed surface (extends 009 SC-003 to the save round-trip; verified by extending the existing secret-hygiene unit tests).
- **SC-003**: The `GET /api/config/services` response shape is byte-identical to 0.9.0 for the same effective config — existing 008/009 contract tests stay green unchanged.

## Out of scope

- Clearing/deleting saved credential values from the panel (overwrite-only, per 009).
- Displaying current credential values anywhere in the UI (write-only posture).
- Credential rotation, expiry, or per-service key management UI.
- Any change to MCP/CLI config paths — they remain equivalent config-layer surfaces (BR-12.2.1).
