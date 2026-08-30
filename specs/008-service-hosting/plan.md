# Implementation Plan: Service Dependencies & Hosting Bootstrap

**Branch**: `008-service-hosting` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/008-service-hosting/spec.md`

## Summary

Extend digital-twins (v0.7.0) with three things: (a) **fail-fast service-dependency gating** — all four hard services (Qdrant, Neo4j, LLM, embedding) checked before any ingestion run on every trigger path, with `validate` distinguishing unconfigured / unreachable / auth-failed and naming the exact knob; (b) **first-class external hosting** — endpoint + credential knobs for all four services usable from the config layer, plus a new admin-gated `GET/POST /api/config/services` web surface that persists to machine-local `kb.local.yml` and never echoes credentials; (c) **shipped `scripts/bootstrap-local.sh`** — one invocation on a clean Docker host pulls the pinned images, builds the app image, starts the stack (skipping the bundled LLM on no-GPU hosts per BR-12.3.4), waits for health, and writes machine-local config; idempotent on re-run; host-neutral (portability-guarded).

Technical approach in one line: one preflight gate inside `run_pipeline` (all four trigger paths route through it — cli.py:328, mcp/dispatch.py:376, scheduler/loop.py:138, web `/api/ingest/run`), a `status` field on `HealthResult`, two new embedding knobs + endpoint-aware embedder (stdlib urllib, no new deps), a machine-local config writer, two admin-gated web routes, one ~150-line bash script. Test-first throughout (Constitution III).

## Technical Context

**Language/Version**: Python `requires-python >= 3.11` (package manifest; no host interpreter pin — Constitution I); bash for the bootstrap script (bash ≥ 4, no exotic features).

**Primary Dependencies**: no new runtime dependencies — endpoint-embedding HTTP uses stdlib `urllib` (house style, mirrors `health.py`); YAML already in the config layer. Host prerequisites for bootstrap: Docker + compose plugin (or classic `docker-compose`), nvidia-smi optional (GPU probe).

**Storage**: SQLite state DB — **no schema changes** (`audit_runs`, `accounts` untouched). New file: machine-local `kb.local.yml` write path (config-dir-resolved, already gitignored). Compose named volumes unchanged.

**Testing**: pytest — `tests/unit/` + `tests/integration/`. Standing guards that must stay green: `tests/integration/test_portability.py` (T006, extended to `scripts/`), `tests/unit/test_knob_docs.py` (T027 — every knob documented in `config.example.yml` + `.env.example`), `tests/integration/test_docker_compose.py` (005 well-formedness).

**Target Platform**: Linux/macOS Docker hosts (local mode); CI without Docker (mocked/static bootstrap surface). Web UI unchanged browser story.

**Project Type**: library + CLI + web service (existing) + new shipped shell script.

**Performance Goals**: per-service health probe ≤ 10 s (existing `HTTP_TIMEOUT_S`); bootstrap full cold path ≤ `BOOTSTRAP_TIMEOUT_S` (default 600 s); no hot-path change (endpoint-embedder HTTP only when `embedding.endpoint` is set).

**Constraints**: host-neutrality (NFR-13 — no host paths/usernames/pins in script or docs); secrets never in logs/audit/error output/UI responses (FR-004); `docker-compose.yml` remains the single source of image pins (005 SC-005); no DB migration (VI).

**Scale/Scope**: 4 hard services; 2 new API routes; 1 script; 2 new config knobs; ~5 new test files + 1 modified guard.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Status | Notes |
|---|---|---|---|
| I | Portability & Environment Neutrality | **PASS** | bootstrap script is host-neutral and added to the portability guard scan (R8); new knobs resolve through the config layer only; no interpreter pins (script is bash, no python invocation). |
| II | Deterministic, Idempotent Ingestion | **PASS** | preflight gate performs no ingestion; bootstrap idempotency is explicit (R9: 0 pulls/builds/state writes on healthy re-run); dedup path untouched. |
| III | Test-First (NON-NEGOTIABLE) | **PASS by construction** | tasks order every behavior test-first: gate, status mapping, endpoint embedder, config writer, UI routes, script branches each get RED tests before implementation. |
| IV | Config-First, Fail-Fast | **PASS** | the feature *is* this principle for services: new `embedding.endpoint`/`embedding.api_key` knobs documented in `config.example.yml` + `.env.example` (T027 guard); every failure names the missing prerequisite (BR-12.1.1). |
| V | Auditability & Observability | **PASS** | preflight failures emit structured log lines (service + status + remediation); no audit row is written for a run that never started (Complexity #3); UI writes are admin-gated via the existing role model. |
| VI | Upgrade Safety & Versioning | **PASS** | no schema/migration; bootstrap never overwrites an existing `kb.local.yml` (diff + warn instead, R9); config change is additive (new knobs, defaults unchanged) → minor-bump material, no major. |

**Gate result: PASS — no violations, no deviations requiring complexity justification beyond the four recorded below.**

## Complexity Tracking

*(Constitution: any deviation from the simplest compliant design is recorded here with the named alternative and why it was rejected.)*

1. **Bash bootstrap script instead of a `digital-twins services up` Python subcommand.** Alternative rejected: a subcommand requires host Python + an installed package before the very first bootstrap, breaking the clean-Docker-host story (R1). CLI wrapper stays a follow-up (A7).
2. **LLM 401/403 now fails (ok=False, status=auth-failed)** instead of the legacy lenient "reachable, may still need care". Alternative (keep lenient) rejected: wrong credentials must fail fast on a hard dependency (BR-12.1.1); the lenient message remains available as the `detail` text.
3. **No audit row on preflight failure.** The run never started, so `audit_runs` stays clean; a structured log line carries service + remediation (V). Alternative (write a failed audit row) rejected: it would pollute `kb_run_history` with preflight-only errors and blur the "run happened" invariant.
4. **GPU heuristic = `nvidia-smi -L`, overridable via `LLM_SERVICE=local|skip`.** Named ceiling (ponytail:): exotic/broken-GPU setups can mis-detect; the env override is the escape hatch, and post-hoc config (`KB_LLM__ENDPOINT` external) always works regardless of detection.

## Project Structure

### Documentation (this feature)

```text
specs/008-service-hosting/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output — 9 decisions (R1–R9)
├── data-model.md        # Phase 1 output — ServiceDependency view, LocalConfigWrite, ConfigServiceView
├── quickstart.md        # Phase 1 output — 5 validation scenarios
├── contracts/
│   ├── web-config-api.md   # GET/POST /api/config/services (admin-gated, masked)
│   └── bootstrap-cli.md    # script usage, env overrides, exit codes, behaviors
├── checklists/
│   └── requirements.md     # /speckit.specify quality checklist (all green)
└── spec.md
```

**Structure Decision**: keep the existing single-package layout (Option 1) — one new top-level entry (`scripts/`), one new config module (`config/local_io.py`), and targeted modifications in place. No new trees, no new services.

### Source Code (repository root)

```text
scripts/
└── bootstrap-local.sh            # NEW — FR-005..008 (R1, R2, R9)

digital_twins/
├── health.py                     # MODIFY — HealthResult.status; auth-failed mappings (qdrant 401/403, neo4j AuthError, llm 401/403); new check_embedding; run_health_checks → 4 services (R4, R5)
├── config/
│   ├── schema.py                 # MODIFY — embedding.endpoint + embedding.api_key in known_subs
│   ├── knobs.py                  # MODIFY — register the 2 new knobs (T027 surface)
│   ├── loader.py                 # MODIFY — expose local_config_path() (single source for the writer)
│   └── local_io.py               # NEW — merge_write(updates): atomic read-modify-write of kb.local.yml, preserves unrelated keys, refuses out-of-dir targets (R6)
├── ingest/
│   ├── pipeline.py               # MODIFY — preflight gate at top of run_pipeline → ServiceDependencyError(service, status, remediation); structured log on failure, no audit row (R3)
│   └── embedding.py              # MODIFY — endpoint-aware embedder: OpenAI-compatible /v1/embeddings via urllib when embedding.endpoint set; in-process path unchanged + default (R5)
├── web/app.py                    # MODIFY — GET/POST /api/config/services, admin-gated, masked credential view, env_overrides note (R7; contracts/web-config-api.md)
├── cli.py                        # UNCHANGED — run/serve validate behavior inherits the gate via run_pipeline
├── mcp/dispatch.py               # UNCHANGED — inherits the gate
└── scheduler/loop.py             # UNCHANGED — inherits the gate

tests/
├── unit/
│   ├── test_preflight_gate.py    # NEW — 4/4 services down-able; no audit row; all trigger paths covered
│   ├── test_health_status.py     # NEW — status enum incl. auth-failed mappings
│   ├── test_config_local_io.py   # NEW — atomic write, key preservation, unparseable → no write
│   ├── test_config_credentials.py# NEW — env-only external endpoints reach validate AND ingestion clients; no value in logs/audit fixtures
│   ├── test_bootstrap_script.py  # NEW — mocked docker/exec surface: pull/skip/fail-fast/no-GPU/idempotent branches, exit codes
│   └── test_knob_docs.py         # EXISTING guard — stays green (documents the 2 new knobs)
└── integration/
    ├── test_web_config_api.py    # NEW — admin GET/POST, 403 non-admin, masking, 404/422/409, persistence
    └── test_portability.py       # MODIFY — SHIPPED_NON_PY += scripts/bootstrap-local.sh; add python-pin pattern (R8)

config.example.yml                # MODIFY — document embedding.endpoint / embedding.api_key (T027)
.env.example                      # MODIFY — document KB_EMBEDDING__ENDPOINT / KB_EMBEDDING__API_KEY
```

## Phase 0 — Research

Complete: [research.md](./research.md) (R1–R9; all NEEDS CLARIFICATION resolved — none remained in the Technical Context after the clarify decisions of 2026-08-30).

## Phase 1 — Design & Contracts

- Entities & state: [data-model.md](./data-model.md)
- Interface contracts: [contracts/web-config-api.md](./contracts/web-config-api.md), [contracts/bootstrap-cli.md](./contracts/bootstrap-cli.md)
- Validation guide: [quickstart.md](./quickstart.md)
- Agent context: `AGENTS.md` SPECKIT marker updated to this plan.

**Constitution re-check post-design: PASS** (no new deviations introduced by the design; Complexity Tracking unchanged).

**Next**: `/speckit.tasks` — break this plan into ordered, test-first tasks (SDD ledger in `.superpowers/sdd/` per repo convention).
