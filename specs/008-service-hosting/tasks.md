# Tasks: Service Dependencies & Hosting Bootstrap (008)

**Input**: Design documents from `/specs/008-service-hosting/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅

**Tests**: MANDATORY — Constitution Principle III (Test-First, non-negotiable): every phase lists the RED test task(s) before the implementation task(s) they drive.

**Story map**: US1 P1 (fail-fast, 🎯 MVP) · US2 P1 (external hosting + UI) · US3 P2 (bootstrap script) · US4 P3 (config-only mode switch)

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on other in-flight tasks)
- **[Story]**: owning user story (F = foundational/shared)
- Every task names exact file paths.

## Phase 1: Setup

**Purpose**: repo is already initialized (v0.7.0); record-keeping only

- [x] T001 [F] Create SDD ledger: `.superpowers/sdd/008-service-hosting/progress.md` (DONE at tasks generation — verify it exists) with a per-task ledger to update as tasks complete
- [x] T002 [F] Baseline anchor: run `pytest -q` (831 tests at v0.7.0) and record the pre-change count/result in `progress.md`; the two standing guards `tests/integration/test_portability.py` and `tests/unit/test_knob_docs.py` must be green before any change

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: config-surface additions every story consumes — embedding knobs (US1/US2/US3) and the machine-local writer (US2/US3)

**⚠️ CRITICAL**: no user-story work until this phase is complete

- [x] T003 [US1] (RED) Write `tests/unit/test_embedding_knobs.py`: `embedding.endpoint` + `embedding.api_key` accepted by `load()`/schema (unset → defaults), env forms `KB_EMBEDDING__ENDPOINT`/`KB_EMBEDDING__API_KEY`, unknown-key rejection intact; assert T027 doc expectations (default + env lines present in examples)
- [x] T004 [P] [US1] Implement the two knobs: `digital_twins/config/schema.py` (`embedding` known_subs), `digital_twins/config/knobs.py` (register), `config.example.yml` + `.env.example` (document default + env — T027 guard), check `docs/configuration.md` knob table for a matching row if it exists
- [x] T005 [US2] (RED) Write `tests/unit/test_config_local_io.py`: `local_config_path()` resolves the same file the loader reads (`config_dir/kb.local.yml`); `merge_write` round-trips, preserves unrelated keys, is atomic (no partial file on exception), aborts without write when existing YAML is unparseable, refuses out-of-dir targets
- [x] T006 [P] [US2] Implement `digital_twins/config/local_io.py` (`local_config_path` delegating to a new `digital_twins/config/loader.py` export) and the `merge_write` upsert
- [x] T007 [F] Checkpoint: `pytest tests/unit/test_embedding_knobs.py tests/unit/test_config_local_io.py tests/unit/test_knob_docs.py -q` green → record in `progress.md`

## Phase 3: User Story 1 — Fail fast on missing or unhealthy services (Priority: P1) 🎯 MVP

**Goal**: every trigger path refuses to run when any hard dependency is unconfigured/unreachable/auth-failed; `validate` names the service + exact knob (FR-001, FR-002; SC-001)

**Independent Test**: in fixtures bring each of qdrant/neo4j/llm/embedding down or unset one at a time; `validate` output names that service with status + remediation; `run --once` / scheduler / MCP / web trigger exit non-zero with **no** audit row marked successful — 0 silent no-op runs.

### Tests for User Story 1

- [x] T010 [P] [US1] (RED) `tests/unit/test_health_status.py`: `HealthResult.status` enum (`ok|unconfigured|unreachable|auth-failed`, optional field, legacy consumers unaffected); mapping — qdrant 401/403 → auth-failed; neo4j `AuthError` → auth-failed; llm HTTP 401/403 on `/models` → auth-failed with `ok=False`; unset endpoint → unconfigured; network error → unreachable (each with remediation naming the knob)
- [x] T011 [P] [US1] (RED) `tests/unit/test_health_embedding.py`: `check_embedding` — endpoint set → probes `{endpoint}/v1/models` (mock urllib; ok / unreachable / auth-failed); endpoint unset → in-process check via `model_dimension` (ok with dim detail); wrong dim vs collection still reported by the qdrant check
- [x] T012 [P] [US1] (RED) `tests/unit/test_preflight_gate.py`: `run_pipeline` raises `ServiceDependencyError(service, status, remediation)` before any write when any of the 4 checks is not ok; **no audit row** written on preflight failure; structured log line emitted with service+remediation (and no credential values)
- [x] T013 [P] [US1] (RED) `tests/integration/test_failfast_paths.py`: gate observed at every trigger path — CLI `run --once` (non-zero exit, stderr names service), scheduler `loop.py` tick (failure surfaced, no successful audit, loop survives), MCP dispatch ingest tool (error dict, no audit), web `/api/ingest/run` (5xx-shaped error, no audit); `serve` performs the preflight at startup and exits non-zero when a hard dep is down (US1 AC3 incl. serve)

### Implementation for User Story 1

- [x] T014 [US1] Implement `digital_twins/health.py`: `status` field on `HealthResult`; auth-failed mappings in `check_qdrant`/`check_neo4j`/`check_llm` (llm 401/403 → ok=False; keep the legacy sentence in `detail`); new `check_embedding`; `run_health_checks` returns 4 services; add `ServiceDependencyError` + a `preflight(cfg)` helper that raises on the first non-ok with the named remediation
- [x] T015 [P] [US1] Implement `digital_twins/ingest/pipeline.py`: call `preflight(cfg)` at the top of `run_pipeline`; on failure log structured (service/status/remediation) and raise — no audit row, no partial writes
- [x] T016 [P] [US1] Wire startup preflight into `digital_twins/cli.py` `serve` (non-zero exit + named service) and confirm `validate`/`health` rendering shows the 4-service status table with `status` + remediation (update `contracts/cli.md` if the output shape changed)
- [x] T017 [US1] Story checkpoint: `pytest tests/unit/test_health_status.py tests/unit/test_health_embedding.py tests/unit/test_preflight_gate.py tests/integration/test_failfast_paths.py -q` green; run one manual `digital-twins validate` with `KB_QDRANT__URL=` unset → UNHEALTHY + named knob. Record in `progress.md`. **MVP increment deliverable**

## Phase 4: User Story 2 — External services via endpoints + tokens (Priority: P1)

**Goal**: endpoints + credentials flow from the config layer to **every** client (validate AND ingestion), endpoint-embedding works, and the admin UI can set all four services with secret-safe persistence (FR-003..004, FR-010; SC-002)

**Independent Test**: set external endpoint + credential via env only → `validate` + one-shot run succeed; grep captured logs/audit for the credential value → 0 hits; admin UI POST persists to `kb.local.yml` masked.

### Tests for User Story 2

- [x] T020 [P] [US2] (RED) `tests/unit/test_config_credentials.py`: env-only external `qdrant.url`+`qdrant.api_key`, `neo4j.url`+`neo4j.user`+`neo4j.password`, `llm.endpoint`+`llm.api_key` reach the actual client construction in both health checks and ingestion paths (capture the client kwargs/headers via fixtures; assert the credential value arrives and no shipped file was touched)
- [x] T021 [P] [US2] (RED) `tests/unit/test_secret_hygiene.py`: for each of the 4 services, the credential value is absent from log records, audit row JSON, and error string fixtures (US2 AC2; FR-004); example files still carry placeholders only
- [x] T022 [P] [US2] (RED) `tests/unit/test_embedding_endpoint.py`: endpoint-aware embedder — with `embedding.endpoint` set, `/v1/embeddings` is called via urllib (mock; assert model + auth header, response vectors pass through, dimension validated against collection check expectations); with it unset, in-process `load_embedder` path is unchanged (stub model, same as `_resolve_embedder` tests)
- [x] T023 [P] [US2] (RED) `tests/integration/test_web_config_api.py` (GET): admin → 200 masked view per `contracts/web-config-api.md` (effective merged values, `api_key_set` bools, `env_overrides` list populated when an env var shadows a value); non-admin → 403; missing bearer → 401
- [x] T024 [P] [US2] (RED) `tests/integration/test_web_config_api.py` (POST): partial update → 200 post-write view (submitted key not echoed, `api_key_set: true`); persists to `kb.local.yml` via `merge_write` (unrelated keys preserved); 404 unknown service; 422 schema-invalid; 409 unparseable existing YAML (no write); non-admin 403; request bodies containing keys are not logged (masked-view log only)

### Implementation for User Story 2

- [x] T025 [US2] Verify/fix credential wiring end-to-end: trace each client (`qdrant` client factory, `neo4j` driver, llm enricher, embedder) — wherever a credential knob is defined but not passed to the client, wire it (expectation: mostly already wired since 002/003; fix deltas only, keep diff minimal)
- [x] T026 [P] [US2] Implement `digital_twins/ingest/embedding.py` endpoint path: OpenAI-compatible `/v1/embeddings` via urllib honoring `embedding.endpoint`/`embedding.api_key` (timeout + retry-once on 5xx, house style); `load_embedder`/factory selects endpoint vs in-process; in-process default unchanged
- [x] T027 [P] [US2] Implement `web/app.py` `GET /api/config/services` + `POST /api/config/services` per `contracts/web-config-api.md`: admin gate via `get_role`, masked view builder, `env_overrides` detection (compare env-layer values), 404/422/409 error shapes consistent with existing routes
- [x] T028 [US2] Story checkpoint: `pytest tests/unit/test_config_credentials.py tests/unit/test_secret_hygiene.py tests/unit/test_embedding_endpoint.py tests/integration/test_web_config_api.py -q` green; T027 `test_knob_docs.py` + `test_portability.py` still green. Record in `progress.md`

## Phase 5: User Story 3 — One-script local setup on a clean Docker host (Priority: P2)

**Goal**: `bash scripts/bootstrap-local.sh` on a clean Docker host → healthy stack + machine-local config in one invocation; idempotent; GPU-optional; host-neutral (FR-005..008; SC-003/004)

**Independent Test**: mocked exec surface covers pull / skip-when-present / docker-missing / port-conflict / no-GPU / health-timeout / idempotent-re-run branches with correct exit codes; portability guard scans the script; live proof manual (A8, 005 c4 pattern).

### Tests for User Story 3

- [x] T030 [P] [US3] (RED) Extend `tests/integration/test_portability.py`: `SHIPPED_NON_PY` includes `scripts/bootstrap-local.sh`; add a python-interpreter-pin pattern (`python3\.\d+`); assert the script passes `HOST_PATTERNS` (the file must exist at this point — create it as an executable stub in the same commit, see T032 ordering note)
- [x] T031 [P] [US3] (RED) `tests/unit/test_bootstrap_script.py` with a mocked docker/exec surface (script's external commands injected via env `BOOTSTRAP_EXEC` override or subprocess monkeypatch — decide mechanism in T032, tests pin it): branches + exit codes per `contracts/bootstrap-cli.md` — docker missing → 1 + remediation, no config write; port 6333 in use → 2 + service named; no-GPU (nvidia-smi absent) → llm skipped, exit 0, `kb.local.yml` written without embedding endpoint (in-process default), SKIPPED line with external-LLM guidance; GPU present → full stack; health timeout → 3 + `docker compose logs` hint, no config write; healthy re-run → 0 pulls, 0 builds, 0 state writes, per-service status, exit 0; `kb.local.yml` exists with disagreeing endpoints → diff + warn, file untouched
- [x] T032 [P] [US3] (RED) `tests/integration/test_bootstrap_pins.py`: the script contains no image reference of its own (005 SC-005 — compose file stays the single pin source); `docker-compose.yml` well-formedness guard (`test_docker_compose.py`) unaffected
- [x] T033 [US3] Implement `scripts/bootstrap-local.sh` (bash, ~150 lines, host-neutral): arg parsing (`--status`), docker + compose detection (plugin or classic `docker-compose` — report available CLI), port-conflict probe (6333/7474/7687/8000/8080), GPU probe (`nvidia-smi -L` + `LLM_SERVICE` override), pin-hash + image-presence check (state in a local, untracked dir — never the repo), `docker compose pull` (public refs) + `build digital-twins` (only when needed), `up -d <services>` (llm excluded when skipped), health poll loop (`BOOTSTRAP_TIMEOUT_S`, default 600), `kb.local.yml` write-only-when-absent (gitignored config-dir path), `NEO4J_PASSWORD` reference-default warning (value never logged), exit codes 0/1/2/3/4, `--status` + `--help` (help documents `docker compose down` teardown per A10)
- [x] T034 [P] [US3] Static checks: `bash -n scripts/bootstrap-local.sh` (+ shellcheck where available — record result in `progress.md`); add the one-liner to README quick start (A7); confirm `docs/configuration.md`/README mention the no-GPU external-LLM path (BR-12.3.4)
- [x] T035 [US3] Story checkpoint: `pytest tests/unit/test_bootstrap_script.py tests/integration/test_portability.py tests/integration/test_bootstrap_pins.py tests/integration/test_docker_compose.py -q` green + `bash -n` clean. Record in `progress.md`

## Phase 6: User Story 4 — Switch hosting mode by config only (Priority: P3)

**Goal**: local ↔ external per service is a config-only change with identical pipeline behavior (FR-009)

**Independent Test**: run `validate` + one ingestion against the local stack; point one service at an external instance via machine-local config only; re-run; assert identical audit shape / point count for the same content (NFR-14 top check pattern).

- [x] T040 [P] [US4] (RED) `tests/integration/test_mode_switch.py`: fixture A local services (stubbed clients) vs fixture B same services with `kb.local.yml` external overrides — identical point count + audit shape for identical content; assert no code path reads the hosting mode anywhere (config layer is the only branch point)
- [x] T041 [US4] Implement any gap T040 exposes (expected: none or minimal — the gate + config layer should already make this hold; record the verdict in `progress.md`)
- [x] T042 [US4] Story checkpoint: `pytest tests/integration/test_mode_switch.py -q` green. Record in `progress.md`

## Phase 7: Polish & Cross-Cutting

- [x] T050 [F] Full suite: `pytest -q` — all 831 baseline + new tests green; standing guards (`test_portability.py`, `test_knob_docs.py`, `test_docker_compose.py`) green; record final count in `progress.md`
- [x] T051 [F] Work `quickstart.md` scenarios 1–4 as runnable proof (4–5 are manual-Docker, record which were run vs deferred per A8); scenario outputs summarized in `progress.md`
- [x] T052 [F] Spec cross-check: re-read spec.md FR-001..010 + SC-001..004 against the diff; mark satisfied FRs in `progress.md`; fix any gap as a new task (loop back to its story phase)
- [x] T053 [F] Run kb.diff-reviewer on the full diff; verdict "OK" or "OK with notes" required before merge; record the verdict + notes in `progress.md`
- [x] T054 [F] Check off every task in `tasks.md` as verified; commit with the SDD ledger updated

## Dependencies & Execution Order

```
T001,T002 ──► T003 ──► T004 ──┐
            T005 ──► T006 ──┤
                             ▼ T007 checkpoint
US1:  T010 ∥ T011 ∥ T012 ∥ T013 (tests) ──► T014 ──► T015 ∥ T016 ──► T017 (MVP ✅)
US2:  T020 ∥ T021 ∥ T022 ∥ T023 ∥ T024 (tests) ──► T025 + (T026 ∥ T027) ──► T028
       (US2 can start after Phase 2 checkpoint; needs T004/T006)
US3:  T030 ∥ T031 ∥ T032 (tests) ──► T033 ──► T034 ──► T035   (independent of US1/US2; needs Phase 2 only)
US4:  T040 ──► T041 ──► T042   (needs US1+US2 complete — gate + credential wiring must hold first)
Polish: T050 → T051 → T052 → T053 → T054   (all stories complete)
```

**Story-level order**: US1 (MVP) → US2 → US3 ∥ US4 after US2 → Polish.

## Parallel Execution Examples

- **Phase 2**: `T004` (knobs: schema/knobs/examples) ∥ `T006` (local_io: new module + loader export) — disjoint files
- **US1**: all four RED tests `T010 ∥ T011 ∥ T012 ∥ T013` in one pass; then `T015 ∥ T016` (pipeline vs cli/validate)
- **US2**: `T025` (credential wiring audit) ∥ `T026` (embedding endpoint) ∥ `T027` (web routes) — disjoint files
- **US3**: `T030 ∥ T031 ∥ T032` in one pass; `T033` is single-threaded (one script); `T034` ∥ any review of T033
- **Across stories (one session, subagents)**: US1 and US3 are file-disjoint after Phase 2 → can run in parallel subagents; US2 and US3 also disjoint (web vs script)

## Implementation Strategy

- **MVP first**: complete Phases 1–3 (US1) and stop → fail-fast is usable and independently testable (checkpoint T017). This is the smallest trust-improving increment.
- **Incremental delivery**: + US2 (external hosting + UI, T028 checkpoint) → + US3 (bootstrap, T035) → + US4 (parity assertion, T042) → Polish. Each checkpoint is green-suite + recorded evidence.
- **Full feature**: all phases; diff-reviewer gate (T053) before merge; tasks.md checkboxes kept in sync with commits (repo SDD convention).
