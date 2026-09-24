# Capability: admin-surfaces — web admin Channels panel

## ADDED Requirements

### Requirement: Admin web UI shows a Channels panel
The admin web UI SHALL include a "Channels" panel (alongside the existing
"Services" panel) that lists every known source with:
- a toggle (enabled / disabled),
- `max_items` and `timeout_s` numeric inputs,
- a credential status indicator (set / not-set — never the value),
- a prerequisites warning (names each missing prerequisite per BR-11.2.2).

The panel SHALL load data from `GET /api/config/channels` and submit
changes via `POST /api/config/channels`. The panel SHALL be admin-gated
(the page itself requires admin sign-in per BR-11.4.4; the API enforces
the same gate).

#### Scenario: Admin opens Channels panel
- **WHEN** an admin user signs in to the web app and opens the Channels
  panel
- **THEN** the panel renders one row per known source showing its
  enabled toggle, cap/timeout values, credential-set indicator, and
  prerequisite warnings, and the data matches the `GET
  /api/config/channels` response.

#### Scenario: Non-admin is denied the panel data
- **WHEN** a non-admin user's browser requests `GET /api/config/channels`
- **THEN** the response is 403 (admin gate, same as
  `/api/config/services`).

### Requirement: Channels panel save persists without file edits
Saving the Channels panel SHALL write to `kb.local.yml` (machine-local,
untracked) via the same `merge_write` path as the Services panel — no
committed-file edit required (BR-12.2.1: external / partial hosting is
completable without file edits). The post-save UI SHALL re-fetch and
display the masked view (credentials never shown).

#### Scenario: Admin saves a channel change in the panel
- **WHEN** an admin toggles `hermes` enabled and clicks Save in the
  Channels panel
- **THEN** the browser POSTs to `/api/config/channels`, `kb.local.yml`
  is updated, no committed file is touched, and the panel re-renders
  with the masked post-write values.

### Requirement: Documentation covers all channel knobs
`docs/configuration.md` SHALL document every channel knob: the per-source
`enabled` / `max_items` / `timeout_s` / `credential` / `entrypoint` /
`prefix` fields, the `channels` CLI subcommand set, and the
`/api/config/channels` admin endpoints, grouped by source as in the
existing services documentation (BR-11.2.4 — no knob may exist that is
not documented).

#### Scenario: Knob-doc sync guard passes
- **WHEN** the standing guard `tests/unit/test_knob_docs.py` (T027) is
  run after this change
- **THEN** every `sources.<name>.*` knob exposed in the CLI, web API,
  and config layer is present in `docs/configuration.md` and the test
  passes without listing any undocumented knob.

## MODIFIED Requirements
(none)

## REMOVED Requirements
(none)
