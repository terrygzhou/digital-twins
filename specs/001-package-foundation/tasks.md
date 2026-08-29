# Tasks: Portable Package Foundation

**Input**: Design documents from `/specs/001-package-foundation/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

**Tests**: Included — the user story independent tests, the measurable success criteria (SC-001…SC-006), and Constitution III (Test-First, NON-NEGOTIABLE) require them. Each story's tests are written FIRST and must FAIL before implementation.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)

## Path Conventions

Single project: `digital_twins/` and `tests/` at repository root (per plan.md structure).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [x] T001 Create package scaffold: `pyproject.toml` (hatchling; deps click, PyYAML, python-dotenv, qdrant-client, neo4j, sentence-transformers, torch; `requires-python = ">=3.11"`; console script `digital-twins` → `digital_twins.cli:main`; MIT `LICENSE`) in `pyproject.toml`
- [x] T002 [P] Create package skeleton: `__version__` single-sourced in `digital_twins/__init__.py`, `python -m` entry in `digital_twins/__main__.py`, click group with `--version` in `digital_twins/cli.py`
- [x] T003 [P] Create shipped config examples — every knob documented, grouped, all sources `enabled: false` — in `config.example.yml` and `.env.example`
- [x] T004 [P] Create test scaffold: shared fixtures (in-memory/local Qdrant, stubbed Neo4j + LLM transports) in `tests/conftest.py`; empty `tests/unit/` and `tests/integration/`
- [x] T005 [P] Create initial `CHANGELOG.md` (0.1.0) and `README.md` skeleton (title, install, command list, config-reference placeholder)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T006 [P] Write portability invariant test — automated scan of shipped code, config defaults, and docs for host paths/usernames/install locations (SC-003, NFR-13) in `tests/integration/test_portability.py` (FAILS until the package exists to scan; must stay green in every later phase)
- [x] T007 Implement typed config model + validation for the full knob surface in `digital_twins/config/schema.py` (per `contracts/config-schema.md`)
- [x] T008 Implement four-layer config loader — env (incl. `.env`) → `kb.local.yml` → `kb.yml` → built-in defaults, deep-merge, env wins; `KB_CONFIG_DIR`/`KB_STATE_DIR` path resolution — in `digital_twins/config/loader.py`
- [x] T009 Implement state layer: sqlite (WAL) connect + state-dir resolution in `digital_twins/state/db.py`; tables `accounts`, `highwater(source, item_key, last_key, updated_at)`, `audit_runs` in `digital_twins/state/models.py`
- [x] T010 Implement `PRAGMA user_version` migration runner that completes before any command in `digital_twins/state/migrations.py`; wire into the CLI entry path in `digital_twins/cli.py`
- [x] T011 Implement deterministic point-ID scheme (prefix + item key + content hash) in `digital_twins/ingest/ids.py` and config-driven chunking in `digital_twins/ingest/chunking.py`
- [ ] T012 [P] Implement pinned `BAAI/bge-small-en-v1.5` embedding loader with `embedding.device` knob (`auto|cpu|cuda`) and model-dimension accessor in `digital_twins/ingest/embedding.py`

**Checkpoint**: Foundation ready — config resolves, state migrates, pipeline primitives exist. User story implementation can now begin.

---

## Phase 3: User Story 1 - Clean-host install, init, and health check (Priority: P1) 🎯 MVP

**Goal**: A fresh host installs, runs `init` (endpoints → starter config with all sources disabled → state + account DB), and gets a passing `validate` health report — no host-specific files (US1, FR-001/003/006, NFR-12).

**Independent Test**: quickstart Scenarios 1–2 against in-memory Qdrant + stubbed Neo4j/LLM: init creates the all-disabled starter config and state, re-running init is idempotent, validate reports pass/fail per endpoint with remediation hints and exits 0.

### Tests for User Story 1 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T013 [P] [US1] Write init behavior tests — creates starter `kb.local.yml` with every source disabled, creates state dir + `state.db`, idempotent re-run, interrupted-init resume (edge case) — in `tests/unit/test_init.py`
- [ ] T014 [P] [US1] Write validate tests — healthy against in-memory Qdrant + stubs, per-endpoint pass/fail + remediation hint, exit codes 0/1 — in `tests/integration/test_validate.py`

### Implementation for User Story 1

- [ ] T015 [US1] Implement endpoint health checks (Qdrant reachable + collection check, Neo4j reachable + auth, LLM endpoint reachable) with remediation hints in `digital_twins/health.py`
- [ ] T016 [US1] Implement `init` command — prompt or read endpoints from config/env, `--yes` flag, create config dir + starter `kb.local.yml` (all sources disabled), create state dir + DB, run validation, idempotent/resumable — in `digital_twins/cli.py` (depends on T008, T009, T015)
- [ ] T017 [US1] Implement `validate` command — health table (endpoint, ok/fail, detail, remediation), exit 0 only when all configured checks pass — in `digital_twins/cli.py` (depends on T015)

**Checkpoint**: US1 fully functional — clean-host install → init → passing validation, independently testable (quickstart S1–S2).

---

## Phase 4: User Story 2 - Environment decoupling: no host coupling, fail-fast enablement (Priority: P1)

**Goal**: Sources are named, configurable channels with capability declarations; a source enabled without its prerequisite fails fast and names it (never a silent zero-item ingest); `run` performs real dedup-safe ingestion with audit (US2, FR-004/007/008/013, BR-11.2.2).

**Independent Test**: quickstart Scenarios 3–4 + 6: `fs` source ingests two files, second run skips both (one point per item, Constitution II); hermes enabled without its session store → exit 2 naming the prerequisite; portability scan (T006) stays green.

### Tests for User Story 2 ⚠️

- [ ] T018 [P] [US2] Write idempotency invariant test — ingest, re-ingest, assert exactly one point per item + high-water marks + audit rows (the one-record-not-N invariant) — in `tests/integration/test_idempotency.py`
- [ ] T019 [P] [US2] Write fail-fast tests — enabled source with missing prerequisite fails at enable-time and run-time, exit 2, error names source + missing prerequisite + where to set it; nothing ingested; failed run still audited — in `tests/integration/test_failfast.py`

### Implementation for User Story 2

- [ ] T020 [US2] Implement source contract — `Capability` (runtime, credential, prefix), `Source` (`prerequisites()`, `read(since)`, `close()`), built-in registry — in `digital_twins/sources/base.py` (per `contracts/source.md`)
- [ ] T021 [P] [US2] Implement `fs` directory-of-files source (demo/test double) in `digital_twins/sources/fs.py` (depends on T020)
- [ ] T022 [P] [US2] Implement session-store sources `hermes`, `pi`, `dsh` — reimplemented inside the package (baseline scripts are reference only), each `enabled: false` by default with declared prerequisites — in `digital_twins/sources/hermes.py`, `digital_twins/sources/pi.py`, `digital_twins/sources/dsh.py` (depends on T020)
- [ ] T023 [P] [US2] Implement `paperclip` PG chat source in `digital_twins/sources/paperclip.py` and shared IMAP source with `yahoo`/`gmail` instances + credential capability — in `digital_twins/sources/imap_mail.py` (depends on T020)
- [ ] T024 [US2] Implement ingestion pipeline — read → chunk → embed → upsert Qdrant (deterministic IDs) + Neo4j nodes/links → high-water marks → one audit row per run — in `digital_twins/ingest/pipeline.py` (depends on T011, T012, T020, T021)
- [ ] T025 [US2] Implement `run` command — enable-time + run-time prerequisite fail-fast (exit 2, names the prerequisite), `--source`, `--max-items`, `--dry-run`, per-source counts + `run_id` output, audit row written regardless of outcome — in `digital_twins/cli.py` (depends on T024)

**Checkpoint**: US1 + US2 both functional — the tool configures, refuses cleanly, and ingests dedup-safe with audit.

---

## Phase 5: User Story 3 - Complete, predictable configuration surface (Priority: P2)

**Goal**: Every knob is discoverable in the two shipped example files, grouped by concern, with deterministic documented precedence (US3, FR-006/009, SC-002).

**Independent Test**: quickstart Scenario 6: set the same value in two layers → documented precedence resolves it (debug reports which layer won); audit shipped examples against the live surface → 0 undocumented knobs.

### Tests for User Story 3 ⚠️

- [ ] T026 [P] [US3] Write precedence tests — env > `kb.local.yml` > `kb.yml` > built-in, deterministic, debug layer-wins report (edge: conflicting layers) — in `tests/unit/test_config_precedence.py`
- [ ] T027 [P] [US3] Write knob-doc sync test — example files ↔ `knobs.py` registry, zero undocumented knobs (SC-002) — in `tests/unit/test_knob_docs.py`

### Implementation for User Story 3

- [ ] T028 [US3] Implement machine-readable knob registry (single source of truth for the config surface) in `digital_twins/config/knobs.py` (makes T027 pass)
- [ ] T029 [US3] Finalize `config.example.yml` + `.env.example` grouping (endpoints / embedding / graph / agent-runtimes / email / per-source) and the loader's debug layer-wins output — in `config.example.yml`, `.env.example`, `digital_twins/config/loader.py` (makes T026 pass)
- [ ] T030 [US3] Write the config reference section (layers, precedence, every knob, how to add values) in `README.md`

**Checkpoint**: US3 functional — the config surface is complete, documented, and precedence is deterministic and proven.

---

## Phase 6: User Story 4 - Safe invariants: embedding mismatch, declared runtime, versioning (Priority: P2)

**Goal**: Dimension mismatch hard-fails with remediation; machine-readable version + changelog; in-place upgrades preserve state (US4, FR-005/010/012, NFR-2/15, SC-005/006).

**Independent Test**: quickstart Scenarios 2 (mismatch half) + 7: wrong-dim collection → hard error + remediation, exit 1; `--version` machine-readable; upgrade test passes.

### Tests for User Story 4 ⚠️

- [ ] T031 [P] [US4] Write dimension-mismatch test — collection vector size ≠ configured model dim → hard error with remediation message at validate-time and before first write; 0 silent mismatches (SC-005) — in `tests/integration/test_dim_mismatch.py`
- [ ] T032 [P] [US4] Write upgrade-preservation test — seed state (high-water + audit + accounts), run a version-bumped migration, assert all rows + config survive (SC-006) — in `tests/unit/test_upgrade.py`

### Implementation for User Story 4

- [ ] T033 [US4] Implement embedding dimension guard — compare model dim vs collection vector size, hard-fail with remediation (re-embed, or new collection) — in `digital_twins/health.py` and pre-write check in `digital_twins/ingest/pipeline.py` (makes T031 pass)
- [ ] T034 [US4] Complete `--version` machine-readable output in `digital_twins/cli.py`, finalize `CHANGELOG.md` 0.1.0 entry, and harden `digital_twins/state/migrations.py` per T032

**Checkpoint**: US4 functional — data-safety and upgrade-safety invariants are enforced and proven.

---

## Phase 7: User Story 5 - Extendable source model without a package update (Priority: P2)

**Goal**: A user-defined source declared purely in config (entrypoint + capability + credential + prefix) participates in a run with fail-fast checks — no package update (US5, FR-011).

**Independent Test**: quickstart Scenario 5: custom source ingests with its stamped tag; without its declared credential → exit 2 naming it.

### Tests for User Story 5 ⚠️

- [ ] T035 [P] [US5] Write custom-source tests — config-declared `entrypoint` factory participates in a run (capability checks, credential, stamped `source_url`); missing credential → exit 2; bad entrypoint → fail-fast naming the module — in `tests/integration/test_custom_source.py`

### Implementation for User Story 5

- [ ] T036 [US5] Implement user-defined source loader — import + call `module:factory` from config, validate the returned `Source` against the contract, capability from config entry — in `digital_twins/sources/custom.py` (depends on T020, makes T035 pass)
- [ ] T037 [US5] Write the "add a source" how-to (config entry shape, capability fields, credential convention, prefix) in `README.md`

**Checkpoint**: All user stories independently functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Whole-feature validation and release readiness

- [ ] T038 [P] Polish `README.md` quickstart + validation guide against `quickstart.md` wording; verify every documented command/flag exists
- [ ] T039 Run `quickstart.md` Scenarios 1–7 end-to-end against a local Qdrant instance; fix any drift between docs and behavior
- [ ] T040 [P] Final audit pass — portability scan (T006) + knob-doc audit (T027) green on the shipped tree; tag `v0.1.0`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Stories (Phases 3–7)**: All depend on Foundational; stories are independently testable and may proceed in parallel (if staffed) or sequentially P1 → P2
- **Polish (Phase 8)**: Depends on all desired user stories

### User Story Dependencies

- **US1 (P1)**: After Phase 2 — no story dependencies (init + validate only)
- **US2 (P1)**: After Phase 2 — no story dependencies (own source/pipeline files); recommended right after US1 since both are P1
- **US3 (P2)**: After Phase 2 — touches `loader.py`/examples already drafted in T003/T008; runs cleanly after US1/US2
- **US4 (P2)**: After Phase 2 — guards `health.py`/`pipeline.py`/migrations; run after US2 so the dim guard covers the real pipeline
- **US5 (P2)**: After US2 (needs the source registry + pipeline from T020/T024)

### Within Each User Story

- Tests MUST be written and FAIL before implementation (Constitution III)
- Contracts/models before services; services before CLI wiring
- Story complete (checkpoint green) before the next priority

### Parallel Opportunities

- Phase 1: T002–T005 all [P]
- Phase 2: T006 and T012 [P]
- Each story: its test tasks [P] together; US2 source files T021/T022/T023 [P] together; US3 tests T026/T027 [P]; US4 tests T031/T032 [P]
- With subagents: US1 and US2 can be written test-first in parallel after Phase 2 (disjoint files: `health.py`/`cli.py:init` vs `sources/`/`pipeline.py`), merged sequentially

---

## Parallel Example: User Story 2

```text
# Launch tests together (disjoint files):
Task: "Write idempotency invariant test in tests/integration/test_idempotency.py"
Task: "Write fail-fast tests in tests/integration/test_failfast.py"

# After base.py lands, launch all source adapters together:
Task: "Implement fs source in digital_twins/sources/fs.py"
Task: "Implement hermes/pi/dsh sources in digital_twins/sources/{hermes,pi,dsh}.py"
Task: "Implement paperclip + IMAP sources in digital_twins/sources/{paperclip,imap_mail}.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: quickstart S1–S2 — clean-host init → passing validation
5. Demo: installable, host-neutral, healthy

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. US1 → validate independently → MVP health tool
3. US2 → validate independently (S3–S4, S6) → real dedup-safe ingestion with fail-fast
4. US3 → config surface proven complete (S6)
5. US4 → safety invariants proven (S2-mismatch, S7)
6. US5 → extension pattern proven (S5)
7. Polish (S1–S7 end-to-end, v0.1.0 tag)

### Handoff Note (superpowers)

This task list is the execution plan for the superpowers phase: run `subagent-driven-development` (or `executing-plans`) over it, one task per TDD cycle (red → green → commit), stopping at each checkpoint for `verification-before-completion`.

---

## Notes

- [P] tasks = different files, no dependencies on unfinished tasks
- [Story] labels map every story-phase task to US1…US5 for traceability
- Commit after each task or logical group
- Stop at any checkpoint and validate the story independently before moving on
- T006 (portability scan) is a standing guard: it must stay green through every later phase
