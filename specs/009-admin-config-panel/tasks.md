# Tasks: 009 — Web Admin: Service Configuration & Health Panel

**Input**: Design documents from `/specs/009-admin-config-panel/` (plan.md, spec.md, research.md, data-model.md, contracts/web-config-api.md, quickstart.md)

**Organization**: Tasks grouped by user story (US1 P1, US2 P1, US3 P2). Constitution III (Test-First, NON-NEGOTIABLE) applies: tests for each story are written and observed FAILING (RED) before implementation.

**SDD ledger**: `.superpowers/sdd/009-admin-config-panel/progress.md` — update after each task; keep this file's checkboxes in sync with commits (AGENTS.md).

**Sandbox note**: the integration harness uses real 127.0.0.1 sockets; `pytest` must run in an environment with socket capability (dev host / escalated sandbox), never inside a network-blocked sandbox.

## Phase 1: Setup

- [x] T001 Create `.superpowers/sdd/009-admin-config-panel/progress.md` (ledger) and record the baseline: anchor commit, full-suite count (942 at cbe7274), standing guards (`tests/integration/test_portability.py`, `tests/unit/test_knob_docs.py`) green pre-change.

## Phase 2: Foundational (Blocking Prerequisites)

No tasks — all prerequisites already shipped by 006/008: admin gate (`_require_admin`), config loader + `local_io.merge_write`, `health.check_*` functions, static single-page UI. (Research D1–D7: no new shared infrastructure required.)

## Phase 3: User Story 1 — Fix a service endpoint from the web panel (Priority: P1) 🎯 MVP

**Goal**: Admin-gated Services panel: four rows with effective URL, `*_set` credential flags, env-override indicator, remediation text; URL edit + Save through the existing `POST /api/config/services`; re-render from the post-write view.

**Independent Test**: quickstart.md §2 (steps 2–4): sign up (admin) → panel visible → edit qdrant URL → Save → `kb.local.yml` updated (unrelated keys preserved) → panel re-renders the new value.

### Tests for User Story 1 (RED first) ⚠️

- [x] T002 [P] [US1] RED static-contract tests in `tests/integration/test_web_config_probe.py` (new file, harness mirrored from `tests/integration/test_web_config_api.py`): `GET /` serves index.html containing the Services panel markup — `id="services-panel"`, `hidden` by default, row markers for `qdrant|neo4j|llm|embedding`, per-row Save + Test buttons, `services-test-all-btn`, `services-error`, and the admin-gate marker (`me.role === "admin"`).
- [x] T003 [P] [US1] RED save-mapping tests in `tests/integration/test_web_config_probe.py`: as admin, `POST /api/config/services` with the UI display→knob mapping — `{"qdrant": {"url": …}}`, `{"neo4j": {"url": …}}`, `{"llm": {"endpoint": …}}`, `{"embedding": {"endpoint": …}}` — each → 200 and the subsequent `GET /api/config/services` view shows the new effective `url` (locks the mapping so the UI cannot no-op-write or 422).

### Implementation for User Story 1

- [x] T004 [US1] Implement the Services panel in `digital_twins/web/static/index.html`: new `<section class="card services-panel" id="services-panel" hidden>` in `#kb-view`; inline JS in the existing IIFE — `loadServices()` (GET `/api/config/services`, render rows: URL `<input>` prefilled with effective value, `*_set` badges, env-override badge via `KB_<SERVICE>__` prefix mapping, remediation line), per-row **Save** (POST the knob-mapped single-service body; on 200 re-render from the post-write view; on 4xx show `#services-error` and keep input values), admin gate in `bootstrap()` (`me.role === "admin"` → unhide + load).
- [x] T005 [P] [US1] Panel styles in `digital_twins/web/static/style.css`: `.services-panel`, service-row grid, status-pill base, `.env-override` badge.

**Checkpoint**: US1 fully functional (quickstart §2 steps 2–4 pass; T002/T003 GREEN).

## Phase 4: User Story 2 — Test connectivity from the web panel (Priority: P1)

**Goal**: `POST /api/config/services/probe` (admin-gated, read-only, parallel, bounded) + per-row Test / Test-all buttons with status pills and in-flight suppression.

**Independent Test**: quickstart.md §2 step 5–6: qdrant URL at a dead port → Test shows `unreachable` + remediation; fix URL → Test → `ok`; all four dead → Test all returns every result in ≤ 5 s.

### Tests for User Story 2 (RED first) ⚠️

- [ ] T006 [P] [US2] RED probe-happy-path tests in `tests/integration/test_web_config_probe.py`: monkeypatched `digital_twins.health.check_*` fakes (one per status: ok / unconfigured / unreachable / auth-failed) → `POST /api/config/services/probe` with `{}` → 200, canonical order `qdrant, neo4j, llm, embedding`, fields `status|detail|remediation` exact; with `{"services": ["llm","qdrant"]}` → only those two, request order. Plus one real-network test: `embedding.endpoint` at a local `http.server` answering `/models` → un-mocked probe returns `ok`.
- [ ] T007 [P] [US2] RED probe-error tests in `tests/integration/test_web_config_probe.py`: 400 `invalid JSON body` (body not a JSON object, e.g. `[1,2]` or malformed); 400 `services must be a non-empty list` (`services` present but not a list, or empty); 404 `unknown service "x"` for an unknown name inside `services`.
- [ ] T008 [P] [US2] RED SC-002 budget test in `tests/integration/test_web_config_probe.py`: monkeypatch all four `health.check_*` to `time.sleep(6)` → `POST …/probe` with `{}` → response received with all four results present, each `status=="unreachable"` (timeout line), and total elapsed ≤ 5.0 s (parallel deadline `PROBE_PER_SERVICE_DEADLINE_S = 4.5`).
- [ ] T009 [P] [US2] RED secret-hygiene extension in `tests/unit/test_secret_hygiene.py`: probe all four services with the four obviously-fake credentials configured (existing `make_cfg()` pattern) → assert no credential value appears in the probe response JSON or in captured `digital_twins` log records (FR-006 / SC-003).

### Implementation for User Story 2

- [ ] T010 [US2] Implement the probe route in `digital_twins/web/app.py`: dispatch line in `_dispatch_rest` (`path == "/api/config/services/probe" and method == "POST"` → `_handle_config_probe`); handler = `_require_admin` gate → `_read_json_body` (400) → validate `services` subset (404 `unknown service "<name>"`) → re-read effective config per request (`config.loader.load`) → run requested checks in a `ThreadPoolExecutor` (check funcs resolved by module-attribute lookup at request time for testability) with per-service `future.result(timeout=PROBE_PER_SERVICE_DEADLINE_S)` (module constant `4.5`) → on timeout synthesize `{status: "unreachable", detail: "probe timed out after 4.5 s", remediation: <url knob + env form>}` → 200 `{"services": {…}}` in request order.
- [ ] T011 [US2] Wire Test / Test-all in `digital_twins/web/static/index.html`: per-row **Test** + toolbar **Test all** (single request, all four names); POST `/api/config/services/probe`; update pill + remediation from the response; per-service in-flight flag suppresses duplicate probes and disables the button mid-flight; initial pill `not tested`.

**Checkpoint**: US1 + US2 both functional (quickstart §2 steps 5–6; T006–T009 GREEN).

## Phase 5: User Story 3 — Non-admins see no configuration surface (Priority: P2)

**Goal**: Reader/editor sessions: no panel in the DOM; probe route + config routes 403.

**Independent Test**: quickstart.md §3: non-admin sign-in → panel absent; `curl` probe/config routes with reader token → `403 {"error":"permission_denied"}`.

### Tests for User Story 3 (RED first) ⚠️

- [ ] T012 [P] [US3] RED gating tests in `tests/integration/test_web_config_probe.py`: reader token → `POST /api/config/services/probe` 403 `permission_denied` (existing shape); missing bearer → 401; admin token → 200 (contrast); regression: existing 008 config routes still 403 for reader (covered by `tests/integration/test_web_config_api.py` — keep green).
- [ ] T013 [P] [US3] RED DOM-gating test in `tests/integration/test_web_config_probe.py`: served `index.html` asserts the panel is `hidden` by default and is un-hidden ONLY behind the `me.role === "admin"` check (static assertion of the gate; no panel content reachable without admin role — SC-004).

### Implementation for User Story 3

- [ ] T014 [US3] Verify/close gaps: confirm `_handle_config_probe` routes through `_require_admin` (T010) and the JS gate hides the panel for non-admins (T004/T011); fix any gap found; re-run `tests/integration/test_web_config_api.py` (008 regression) + T012/T013.

**Checkpoint**: All three user stories independently functional.

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T015 [P] Standing guards green: `pytest -q tests/integration/test_portability.py tests/unit/test_knob_docs.py` (T006 / T027) — no host paths in new code/UI, no new knobs.
- [ ] T016 Bump `__version__` to `0.9.0` in `digital_twins/__init__.py` (single source) and add the `CHANGELOG.md` `[0.9.0]` entry (Services panel + probe route + budget + no new knobs/deps).
- [ ] T017 Full suite green: `pytest -q` (expect 942 + new tests), then manual quickstart.md §2–§4 pass on this host.
- [ ] T018 Sync `specs/009-admin-config-panel/tasks.md` checkboxes with commits, complete `.superpowers/sdd/009-admin-config-panel/progress.md` (per-phase evidence + commit SHAs), commit; finish per `finishing-a-development-branch` (project convention: work lands on `main`).

## Dependencies & Execution Order

- **Phase 1 (Setup)**: no dependencies.
- **Phase 2 (Foundational)**: none — 006/008 infrastructure is sufficient.
- **US1 (Phase 3)**: after T001; T002/T003 in parallel (different test concerns, same file → write both RED in one pass); T004 then T005 (T005 parallel-safe, different file).
- **US2 (Phase 4)**: after US1 checkpoint (panel DOM hosts the Test buttons); T006–T009 in parallel; T010 then T011.
- **US3 (Phase 5)**: after US2 (probe route must exist to be gated); T012/T013 in parallel; T014 verifies.
- **Polish (Phase 6)**: after US3; T015/T016 in parallel; T017 then T018.

**Within each story**: tests RED-verified before implementation; one commit per task or logical group; ledger updated per task.

## Notes

- [P] = different files / no dependencies.
- Same-file discipline: `index.html` edits (T004 → T011) are sequential, not parallel.
- `PROBE_PER_SERVICE_DEADLINE_S = 4.5` is a module constant (research D3), not a config knob (T027 stays green).
- No new persistent entities, no state-DB migration, no new dependencies, no new knobs (research D7).
