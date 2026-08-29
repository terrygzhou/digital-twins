# Feature Specification: Scheduled & On-Demand Runs

**Feature Branch**: `002-scheduled-runs`

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "BRD slice BR-11.3 (requirement.md §2): the scheduler surface — `digital-twins serve` (standalone scheduler) and `digital-twins run --once` (stateless one-shot for host cron), per-user/per-source schedules with preset cadences, resumable runs, per-run caps/timeouts, and a health/status endpoint. No cron parser (Q10)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Standalone scheduler service (Priority: P1)

An operator runs `digital-twins serve` as a supervised process (systemd /
supervisord / a Docker container). The scheduler fires runs on their preset
cadences, manages its own queue, and exposes a JSON status endpoint that
agents or a UI can poll.

**Why this priority**: BR-11.3.1 (Q6, DECIDED: "both"). This is the
long-running half of the dual surface; without it, ingestion only happens
when the host cron fires.

**Independent Test**: With one enabled source and a daily schedule, start
`serve`; when the fire time is reached (or immediately if overdue), a run
executes, writes an audit record with `trigger: "schedule"`, and `/status`
reports the fire.

**Acceptance Scenarios**:

1. **Given** a schedule whose fire time has been reached, **When** the `serve` loop ticks, **Then** the pipeline runs for that schedule's owner/source and an audit row is written (`trigger=schedule`, `scheduled_by=<owner>`).
2. **Given** `serve` is running, **When** the operator stops it (SIGTERM), **Then** it exits cleanly and no partially-committed items are re-ingested on restart.
3. **Given** no schedules exist, **When** `serve` runs, **Then** it idles without error or audit spam, and `/status` reports an empty schedule list.

---

### User Story 2 - One-shot host-cron run that never duplicates (Priority: P1)

`digital-twins run --once` performs a single run and exits — suitable for the
host's `cron` / `systemd` / CI. It shares the state store with `serve`, so the
same content ingested by either path yields exactly one point (NFR-1, NFR-14).

**Why this priority**: This is the BRD's top acceptance check — one point, not
four, across trigger paths.

**Independent Test**: Ingest the same content via a `serve`-scheduled run and
via `run --once` (in either order); the vector store holds exactly one point
for it.

**Acceptance Scenarios**:

1. **Given** an enabled source with new items, **When** `run --once` completes, **Then** the items are ingested and an audit row is written.
2. **Given** content already ingested by a `serve` run, **When** `run --once` runs again, **Then** zero new points are created and the audit row reports the zero counts — reported, never silent.
3. **Given** `run --once --as alice` with bad credentials, **When** it starts, **Then** it fails fast with a clear authentication error before any ingestion.

---

### User Story 3 - Per-user, per-source schedules with preset-only cadences (Priority: P2)

Schedules belong to a user and a source ("alice runs hermes daily at 03:00,
gmail daily at 03:30") and are creatable/updatable/deletable. Cadences are a
small set of named presets — `daily`, `hourly`, `weekly`, `monthly`,
`every-N-hours` — which expand to concrete fire times internally; the user
never writes or stores a cron expression (Q10). For users who want real cron,
the package documents a one-line host-cron snippet as the alternative.

**Why this priority**: BR-11.3.2 + BR-11.3.7; the "keep it simple" decision
bounds the whole v1 scheduling UX.

**Independent Test**: Create schedules with every preset; verify deterministic
next-fire expansion and CRUD round-trip through the CLI (API/UI/MCP surfaces
are later slices 003/004).

**Acceptance Scenarios**:

1. **Given** preset `daily` for user alice's hermes source, **When** the schedule is created, **Then** the next fire time is computed deterministically (default fire time 03:00, per A5) and stored on the schedule.
2. **Given** preset `every-N-hours` with N=6, **When** the schedule fires, **Then** the next fire is recomputed from the actual fire time (no drift).
3. **Given** an existing schedule, **When** the owner changes its cadence or pauses it, **Then** the change takes effect on the next tick without restarting `serve`.
4. **Given** a request for a custom recurrence ("weekdays at 09:00"), **When** it is attempted in v1, **Then** the package does not store cron strings and points to the documented host-cron snippet instead (follow-up boundary).

---

### User Story 4 - Resumable, capped runs (Priority: P2)

An interrupted run (killed process, rebooted host) resumes from the last
committed item — never from scratch (NFR-9). Per-run caps (e.g. the 200-email
cap) and timeouts are per-source knobs, not global constants; a source may be
paused, re-enabled, or re-capped without restarting the scheduler.

**Why this priority**: BR-11.3.4 + BR-11.3.5 — the correctness guarantees that
make an unattended `serve` trustworthy.

**Independent Test**: Start a run over N items, kill the process mid-run,
restart (either path); total ingested items is exactly N with no re-ingestion
of committed items.

**Acceptance Scenarios**:

1. **Given** a run killed mid-source, **When** the run resumes, **Then** it continues from the last committed high-water mark.
2. **Given** `max_items: 2` on a source with 5 pending items, **When** a run executes, **Then** exactly 2 items are ingested and the audit status is `partial`.
3. **Given** the operator lowers a cap or pauses a source in config, **When** the next run fires, **Then** the new value applies without a restart.

---

### User Story 5 - Health/status surface (Priority: P3)

`serve` exposes a status endpoint (BR-11.3.6, mirroring BR-3.6) reporting:
next fire time per schedule, last run status per source, and current queue
depth — the surface an agent or UI polls to answer "is ingestion healthy?"

**Why this priority**: Observability for the two higher-priority stories; the
poll target for 004's MCP and 003's UI.

**Independent Test**: `GET /status` returns the documented JSON contract with
all three field groups.

**Acceptance Scenarios**:

1. **Given** two schedules, **When** `/status` is requested, **Then** both schedules appear with their `next_fire_at`.
2. **Given** a failed last run, **When** `/status` is requested, **Then** the last status per source reports `failed` and names the source.
3. **Given** a fire awaiting execution, **When** `/status` is requested, **Then** `queue_depth` reflects it.

## Edge Cases

- No sources enabled at all: a run is a no-op that still writes an audit row (`ok`, zero counts) — never a silent vanishing run (fail-fast reporting, 001 invariant).
- Clock moves backwards: overdue detection must not fire the same schedule in a loop.
- Two `serve` instances on one host: the second fails fast with a clear error (single-writer constraint, A2).
- Reboot with `next_fire_at` in the past: catch-up fires once; deterministic IDs + high-water marks prevent duplicates.
- Host-cron `run --once` and a `serve` fire due in the same minute: both execute; dedup guarantees one point.

## Requirements *(mandatory)*

### Functional Requirements

- FR-1 (BR-11.3.1): `serve` = long-running scheduler (fires on schedule, owns its queue, exposes status); `run --once` = stateless one-shot. Both produce identical ingestion results (same pipeline, same deterministic IDs, same audit record shape).
- FR-2 (BR-11.3.1/NFR-1/NFR-14): both paths share `.kbstate/`; the same content ingested via either or both paths yields exactly one point.
- FR-3 (BR-11.3.2): schedules are per-user, per-source, and editable; v1 CRUD surface is the CLI, with API/UI/MCP surfaces deferred to slices 003/004.
- FR-4 (BR-11.3.3): every run writes an audit record with the BR-5.3 fields plus `run_id`, `scheduled_by` (user or `system`), `trigger` (`schedule`|`manual`|`mcp`|`api`), per-source counts, and `started_at`/`completed_at`.
- FR-5 (BR-11.3.4): runs are resumable; per-source high-water marks persist in the state store; resume continues from the last committed item.
- FR-6 (BR-11.3.5): per-run caps and timeouts are per-source config knobs; pause/re-enable/re-cap take effect without restarting the scheduler.
- FR-7 (BR-11.3.6): a status endpoint reports next fire time per schedule, last run status per source, and current queue depth.
- FR-8 (BR-11.3.7): preset cadences only (`daily`, `hourly`, `weekly`, `monthly`, `every-N-hours`); the package does not parse or store cron strings; a host-cron snippet is documented as the alternative; custom recurrence is a follow-up.
- FR-9: `run --once --as <user>` authenticates against the 001 accounts store (email + password); default `scheduled_by` is `system`.

### Key Entities

- **Schedule** — owner (user or `system`), source, preset cadence, fire time, enabled flag, next-fire timestamp, reserved `acl` field (004 placeholder).
- **AuditRun** — 001 shape, trigger values extended to `schedule`|`manual`|`mcp`|`api`.
- **HighWater** — 001, unchanged.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- SC-001: The same content ingested via `serve` and via `run --once` yields exactly one point, in both orderings — automated check.
- SC-002: A run killed mid-execution resumes with zero duplicate points — automated check.
- SC-003: Every preset expands to a deterministic next-fire time — property-tested against the documented expansion rules.
- SC-004: `/status` returns the full documented contract in <500 ms.
- SC-005: Cap/timeout/pause changes apply on the next run without a restart — automated check.
- SC-006: The 001 portability guard (`tests/integration/test_portability.py`) stays green: no host paths in scheduler code, config, or docs.

## Assumptions

- A1: Accounts already exist from 001 (`accounts` table: email + password_hash + role). 002 only authenticates `--as <user>`; sign-up, roles, and role enforcement are slice 003.
- A2: One `serve` instance per host; the state store is guarded by the SQLite lock plus a pidfile, and a second instance fails fast.
- A3: `/status` is an HTTP JSON endpoint on a configurable port (`scheduler.status_port`; `0` disables it, CLI remains the fallback surface).
- A4: The queue is in-process (v1 is single-process); `queue_depth` counts pending fires.
- A5: The default fire time for `daily`/`weekly`/`monthly` presets is 03:00 — the promoted baseline schedule as a *default value*, not a hard-coded constant (BR-11.3.2).
- A6: `mcp`/`api` trigger values are schema-legal in 002; the MCP and API paths themselves ship in slices 004/003.

## Clarifications

No open questions: all scheduling decisions are locked by Q6 (both surfaces) and
Q10 (presets, no cron parser) in requirement.md §5. One boundary decision
recorded here: `--as <user>` authentication lives in 002 because the host-cron
documented snippet (BR-11.3.7) requires it; the full sign-in/role surface is
003 (A1).
