# Tasks: Scheduled & On-Demand Runs

**Feature**: `specs/002-scheduled-runs` · **Constitution**: Test-First (III) — every implementation task has its red test first and fails before it passes.

## Dependency Graph (user story completion order)

```
Phase 2 (Foundational: presets + schedules + migration)
  ├── US1 serve loop + status        (T006–T008)
  ├── US2 run --once dedup + auth    (T009–T011)   [US1 ⊥ US2 — parallel after Phase 2]
  ├── US3 schedule CRUD CLI          (T012–T014)   [needs Phase 2 only]
  └── US4 resume + caps              (T015–T017)   [needs US1 or US2 pipeline path]
Polish: US5 status contract (T018), docs + standing guards (T019–T020)
```

## Phase 1: Setup

- [ ] **T001** — Scaffold `digital_twins/scheduler/` package (`__init__.py`, empty `presets.py`/`schedules.py`/`loop.py`/`status.py`) + `tests/unit/` and `tests/integration/` test modules importing the new surface (import-only red tests)
- [ ] **T002** — Add `scheduler.status_port` knob to the 001 config schema/defaults (default `8765`, `0` = disabled) + extend `tests/unit/test_knob_docs.py` with the new knob (red until documented)

## Phase 2: Foundational (blocking prerequisites for all stories)

- [ ] **T003** — `presets.py`: `preset_values()` + `expand_next(preset, param, fire_time, now, anchor)` pure function **after** `tests/unit/test_presets.py` property tests (SC-003): fixed-input determinism, all five presets, weekly/monthly anchor rules, month-length clamping, `param` validation (N ≥ 1, `every-N-hours` only)
- [ ] **T004** — State migration v2: `schedules` table per `data-model.md` (incl. `acl` reserved default `'owner'`) + `tests/unit/test_schedules.py` migration/idempotency tests (apply twice = no-op; 001 db unchanged)
- [ ] **T005** — `schedules.py` CRUD: `create_schedule` (upsert on `(owner,source,preset,param,fire_time)`), `list_schedules`, `update_schedule`, `delete_schedule`, `due_schedules`, `claim_and_advance` — each with its red test first (uniqueness, due filtering incl. clock-skew case, atomic advance, invalid-preset fail-fast)

## Phase 3: User Story 1 — Standalone scheduler service (P1) ✅ goal: `serve` fires due schedules with audit + status

- [ ] **T006** `[US1]` — `loop.py::serve_once_tick(db, config)`: due-schedule detection → run 001 pipeline per schedule (shared state, `trigger='schedule'`, `scheduled_by=owner`) → `claim_and_advance`; red test first: one tick over a due schedule produces one audit row and advances `next_fire_at` (in-memory Qdrant, stubbed source)
- [ ] **T007** `[US1]` — `run_serve`: pidfile guard (`serve.lock`, R4/A2) + clean SIGTERM/SIGINT shutdown; red test first: second live instance fails fast with named error; stale pidfile is reclaimed
- [ ] **T008** `[US1]` — `cli.py::serve [--port]` wiring (click) + idle behavior with zero schedules (no audit spam — US1 scenario 3)

## Phase 4: User Story 2 — One-shot host-cron run, never duplicates (P1) ✅ goal: `run --once` shares state, one point total

- [ ] **T009** `[US2]` — `cli.py::run --once [--source]`: single run over enabled sources, `trigger='manual'`, exit codes per `contracts/cli.md`; red test first (new items ingested + audit row)
- [ ] **T010** `[US2]` — **SC-001 top acceptance check**: `tests/integration/test_serve_once_dedup.py` — same content via `run --once` then a served fire, and the reverse order; assert exactly one point each way (red against Phase 3/4 code as it lands; must be green before US1+US2 close)
- [ ] **T011** `[US2]` `[P]` — `--as <user>` auth (R5): verify email+password against 001 `accounts` (env var or interactive prompt, never argv/logs), `scheduled_by=<user>`; `tests/unit/test_auth_as.py` red first (bad password fails fast with zero ingestion, exit code 2)

## Phase 5: User Story 3 — Per-user/per-source schedules, preset-only (P2) ✅ goal: CRUD round-trip + host-cron docs

- [ ] **T012** `[US3]` `[P]` — `cli.py::schedule add|list|remove` per `contracts/cli.md`; red test first: add with each preset → stored row correct; invalid preset lists the five valid ones; list/remove round-trip
- [ ] **T013** `[US3]` `[P]` — `docs/scheduling.md`: preset table with expansion semantics, single-instance note, host-cron snippet block presented as the alternative to `serve` (FR-8; SC-006 keeps it host-neutral)

## Phase 6: User Story 4 — Resumable, capped runs (P2) ✅ goal: kill/restart without duplicates; live cap changes

- [ ] **T014** `[US4]` — `tests/integration/test_resumable_run.py`: 50-item run, SIGKILL at ~20 committed, restart (both paths), assert total == 50 points and audit status recovery (red first; exercises 001 high-water + 002 fire advance)
- [ ] **T015** `[US4]` — Per-run caps/timeouts read **fresh on every fire** (no cached config in `serve`): red test first — mutate `kb.local.yml` `max_items` between fires, assert next fire honors it without restart (SC-005); `max_items=2` of 5 → status `partial`
- [ ] **T016** `[US4]` — Source pause (`enabled: false`) honored mid-serve without restart; red test first (no fire for the paused source, audit reflects the skip)

## Phase 7: User Story 5 — Health/status surface (P3) ✅ goal: `/status` contract

- [ ] **T017** `[US5]` — `status.py`: `status_payload(db, config, pending_fires)` + `ThreadingHTTPServer` handler (`GET /status` → 200 JSON, else 404) per `contracts/scheduler.md`; `tests/unit/test_status.py` red first (payload shape incl. empty-schedules and failed-last-run cases; SC-004 wall-clock assertion: payload computed in <500 ms — assert on the measured duration in the red test)
- [ ] **T018** `[US5]` — Port 0 disables the server entirely (US1 edge); red test first (no listener, `serve` otherwise functional)

## Phase 8: Polish & cross-cutting

- [ ] **T019** — Run `specs/002-scheduled-runs/quickstart.md` scenarios 1–5 end-to-end; fix gaps; record results in the SDD ledger
- [ ] **T020** — Standing guards green: `pytest` full suite, `tests/integration/test_portability.py` (SC-006), `tests/unit/test_knob_docs.py` (T002 knob); version bump decision + changelog entry per 001 release practice

## Notes

- Parallel pairs: T011 ∥ T009/T010 (auth is additive to the one-shot path); T012 ∥ T013; US1 and US2 phases may interleave once Phase 2 lands — T010 is the gate that must close both.
- No host paths anywhere: `serve.lock`, state, and all knobs resolve through the 001 config layer (SC-006 guard is authoritative).
- Task IDs are feature-scoped (this file), matching 001's ledger convention; the SDD ledger (`.superpowers/sdd/`) tracks execution.
