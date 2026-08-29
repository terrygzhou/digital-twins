# Implementation Plan: Scheduled & On-Demand Runs

**Input**: spec at `specs/002-scheduled-runs/spec.md`; 001 artifacts as the compatibility baseline.

## Summary

Add the scheduler surface to the 001 package: a `digital-twins serve` process (stdlib loop + one JSON status route) and the stateless `digital-twins run --once` path, both driving the existing 001 pipeline over the shared `.kbstate/` store so the one-record-not-N invariant holds across trigger paths. Schedules are per-user/per-source rows in a new `schedules` table, preset-only cadences (Q10), resumable runs via the existing high-water marks, and per-source caps/timeouts read fresh on every fire.

Out of scope for this slice (later slices): multi-user sign-up/roles/last-admin enforcement and the web UI (003, BR-11.4), MCP scheduler tools + per-schedule ACLs (004, BR-11.5), Docker/PyPI packaging + community docs (005, BR-11.6), custom cron recurrence (BRD follow-up).

## Technical Context

| Layer | Choice |
|---|---|
| Language | Python ≥3.11 (001 `requires-python`; no new pin) |
| Scheduling | stdlib loop, tick ~3 s; in-process queue (R1, A4) |
| Status endpoint | stdlib `http.server.ThreadingHTTPServer`, one `/status` route (R2, A3) |
| State | 001 SQLite store + migration v2 (`schedules` table); pidfile `serve.lock` (R4) |
| CLI | `click` (001 dependency), new `serve`/`run --once`/`schedule` commands |
| Dedup | 001 deterministic point IDs + high-water marks — no new mechanism |
| Auth | `--as` vs 001 `accounts` (email + password_hash), password via env/prompt (R5) |
| Config | new `scheduler.status_port` knob (default 8765, `0` disables); per-source `max_items`/`timeout_s` from 001 already exist |
| New dependencies | **none** |

## Constitution Check

- **I. Portability & Environment Neutrality** — PASS: no host paths; host-cron snippet is *documentation* of the user's own host, values come from config. 001's `test_portability.py` guard re-run at completion (SC-006).
- **II. Deterministic, Idempotent Ingestion** — PASS: both trigger paths run the identical 001 pipeline over shared state; SC-001/SC-002 are the automated one-record checks, covering *both* new paths.
- **III. Test-First** — PASS: tasks.md orders every red test before its implementation (T002–T014 precede their code).
- **IV. Config-First, Fail-Fast** — PASS: `scheduler.*` knobs; second `serve` instance, bad credentials, invalid preset, missing prerequisites all fail fast with named errors.
- Remaining principles — no conflict (no new data-ownership or doc surfaces beyond the host-cron snippet).

## Project Structure

### Documentation (this feature)

```
specs/002-scheduled-runs/
├── checklists/requirements.md
├── contracts/{scheduler.md, cli.md}
├── data-model.md
├── plan.md
├── quickstart.md
├── research.md
├── spec.md
└── tasks.md
```

### Source Code (repository root)

```
digital_twins/
├── scheduler/            # NEW package
│   ├── __init__.py
│   ├── presets.py        # expand_next() — pure
│   ├── schedules.py      # CRUD + due/claim-advance over 001 db
│   ├── loop.py           # serve_once_tick / run_serve, pidfile guard
│   └── status.py         # status_payload + ThreadingHTTPServer handler
├── state/
│   └── models.py         # + migration v2: schedules table
└── cli.py                # + serve / run --once / schedule add|list|remove
docs/
└── scheduling.md         # NEW: presets, host-cron snippet, single-instance note
tests/
├── unit/
│   ├── test_presets.py           # NEW — property tests for expand_next
│   ├── test_schedules.py         # NEW — CRUD + due + claim_and_advance
│   ├── test_auth_as.py           # NEW — --as acceptance/rejection
│   └── test_status.py            # NEW — payload contract
├── integration/
│   ├── test_serve_once_dedup.py  # NEW — SC-001 both orderings
│   ├── test_resumable_run.py     # NEW — SC-002 kill/restart
│   └── test_knob_docs.py         # 001 guard: new scheduler.* knobs documented
```

## Complexity Tracking

No constitution violations accepted. Deliberate v1 ceilings (all with upgrade paths noted at the decision point):

- Single-process scheduler, in-memory queue — ceiling: one fire at a time; upgrade: worker process (post-v1, if queue depth ever matters).
- No auth on `/status` in 002 — ceiling: local-network exposure; upgrade: 003's token auth reuses the same handler.
- `weekly`/`monthly` anchor to creation time (no TZ handling beyond local time) — ceiling: DST edges use local wall clock; upgrade: IANA tz support if users demand it.
