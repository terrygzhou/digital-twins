# Feature Specification: Service Dependencies & Hosting Bootstrap

**Feature Branch**: `008-service-hosting`

**Created**: 2026-08-30

**Status**: Draft

**Input**: User description: "BRD slice BR-12 (requirement.md §2, v0.3, 2026-08-30): service dependencies & hosting modes — (1) Qdrant, Neo4j, LLM + embedding model are hard runtime service dependencies and must be configured to work; (2) services may be hosted externally (endpoints + access tokens configurable) or locally — Docker Compose is the preferred, supported local mode, and image pull + initial setup must be doable by running shipped scripts included in the codebase (Q11, DECIDED 2026-08-30)."

## User Scenarios & Testing

### User Story 1 — Fail fast on missing or unhealthy services (Priority: P1)

A user runs digital-twins with one or more of Qdrant / Neo4j / LLM / embedding misconfigured (unset) or unreachable (down, wrong endpoint). The tool names exactly which service is failing and how to fix it, and it never completes a run that silently persisted nothing.

**Why this priority**: a silent no-op run is the worst failure mode for an ingestion tool — users trust the audit record. Fail-fast with remediation is the BR-11.2.2 principle extended from ingestion sources to service dependencies (BR-12.1, NFR-18).

**Independent Test**: bring each service down (or unset its endpoint) one at a time; assert `validate` names that service with a remediation message, and `run --once` / `serve` exit non-zero without writing an audit record that claims success.

**Acceptance Scenarios**:

1. **Given** `qdrant.url` is unset, **When** the user runs `digital-twins validate`, **Then** output names Qdrant as unconfigured with the knob to set, and overall health is UNHEALTHY.
2. **Given** the Qdrant endpoint is set but the process is down, **When** `validate` runs, **Then** it reports "unreachable" (distinct from "unconfigured") with a remediation hint.
3. **Given** any required service is unreachable, **When** `digital-twins run --once` runs, **Then** the run fails fast with the named service, writes no audit record marked successful, and exits non-zero.
4. **Given** wrong Neo4j credentials, **When** `validate` runs, **Then** it distinguishes auth-failure from unreachable (both with actionable remediation).

---

### User Story 2 — Point at external services with endpoints + tokens (Priority: P1)

A user with self-hosted or SaaS Qdrant / Neo4j / LLM configures endpoints + access credentials through the config layer (env/.env, kb.local.yml) **or the web admin UI** and gets a working `validate` and first run without touching shipped files. Credential values never appear in committed files, logs, or audit output.

**Why this priority**: BR-12.2.1/2.2 — external hosting is a first-class mode; the credential knobs already exist (`qdrant.api_key`, `neo4j.user`/`neo4j.password`, `llm.api_key`) and must be verified end-to-end and kept secret-safe.

**Independent Test**: set an external endpoint + credential via env only; run `validate` and a one-shot run; grep logs/audit/config evidence for the credential value (must be absent).

**Acceptance Scenarios**:

1. **Given** `KB_QDRANT__URL` + `KB_QDRANT__API_KEY` point at a SaaS Qdrant, **When** `validate` runs, **Then** it reports qdrant reachable using the configured credential — no shipped-file edits.
2. **Given** `KB_NEO4J__PASSWORD` set to a real value, **When** any run or log output is produced, **Then** the value does not appear in logs, audit records, error output, or committed files.
3. **Given** an external LLM endpoint + API key, **When** a run completes, **Then** enrichment used the external endpoint (audit/config evidence) and the key is not echoed.
4. **Given** the web admin UI (admin role), **When** the user sets an external LLM endpoint + API key there, **Then** the values persist to machine-local config, `validate` passes, and the key is not echoed in the UI, logs, or audit records (FR-010).

---

### User Story 3 — One-script local setup on a clean Docker host (Priority: P2)

A community user on a clean host with Docker runs the shipped bootstrap script; it pulls/builds the pinned images, starts the stack, waits for service health, and writes machine-local config with the local endpoints. One invocation → healthy stack → `validate` passes. Re-running against a healthy stack is a no-op that reports per-service status.

**Why this priority**: Q11 (DECIDED) — image pull + initial setup via a shipped script is the supported local-hosting path; it is new work (no `scripts/` exists today) and is this feature's differentiating deliverable.

**Independent Test**: on a Docker host, run the script from scratch (no images) and assert stack healthy + `validate` passes + machine-local config written with local endpoints; re-run and assert no re-pull/re-build and state intact. (CI: static + mocked checks; the live Docker proof is manual, per the 005 c4 pattern.)

**Acceptance Scenarios**:

1. **Given** a clean Docker host (no images, no config), **When** `bash scripts/bootstrap-local.sh` runs, **Then** images are pulled/built (pinned refs only), the stack starts, and the script exits 0 only after every service reports healthy and machine-local config contains the local endpoints.
2. **Given** a healthy stack, **When** the script re-runs, **Then** it does not re-pull, re-build, or reset state, and prints per-service status.
3. **Given** Docker missing or the daemon down, **When** the script runs, **Then** it fails with a remediation message (install/start Docker) and writes no partial config.
4. **Given** a port conflict (e.g. 6333 in use), **When** the script runs, **Then** it names the conflicting service and exits non-zero.
5. **Given** the repo's `docker-compose.yml` / `Dockerfile`, **When** the script runs, **Then** the compose file remains the single source of image pins — the script pins no image on its own (005 SC-005 guard intact).
6. **Given** a host without a suitable GPU, **When** the script runs, **Then** it brings up Qdrant / Neo4j / embedding, skips the bundled LLM service with a warning, and directs the user to configure an external LLM (endpoint + credential via the config layer or the admin UI); the script exits 0 and `validate` reports LLM as pending until configured (BR-12.3.4, owner decision 2026-08-30).

---

### User Story 4 — Switch hosting mode by config only (Priority: P3)

An operator moves a service from the bundled local stack to an external instance (or back) by editing endpoint/credential config only; behavior is identical — no code change, no image rebuild.

**Why this priority**: BR-12.4.1 parity — cheap to guarantee once US1–US3 hold, but must be asserted to prevent drift.

**Independent Test**: run `validate` + one ingestion against the local stack; point one service at an external instance via config only; repeat the run; assert identical audit shape / point count for the same content.

**Acceptance Scenarios**:

1. **Given** a healthy local stack, **When** the user sets Qdrant endpoint + credential to external values in machine-local config only, **Then** the next `validate`/run uses the external instance with identical pipeline behavior.

### Edge Cases

- Partially-up stack (qdrant up, neo4j down): `validate` names exactly the failing service; bootstrap re-run resumes without re-pulling healthy images.
- Embedding dependency shape: when an embedding endpoint knob is configured, `validate` covers the endpoint; otherwise the in-process model-id + dimension check (existing FR-010 pattern) applies.
- Reference-compose default credential (`NEO4J_PASSWORD:-password`): bootstrap MUST surface that the reference default is not secret-grade; it must not log the value.
- Stale machine-local config after switching to external: a local-mode bootstrap re-run rewrites local endpoints but MUST NOT delete unrelated user config keys.
- Docker CLI without the compose plugin (classic `docker-compose` only): bootstrap detects and reports the available CLI, or fails with remediation.
- Local stack whose `embedding-model` service cannot be healthy (tracked 005 defect, A5): bootstrap exits 0 for the remaining services and writes config without an embedding endpoint (in-process default) unless the user set one; the health-gate reports the defect explicitly.
- CPU-only host (Q1 decided 2026-08-30): no suitable GPU → bundled LLM service is skipped, not failed; bootstrap exits 0 with the partial stack and external-LLM guidance; BYO-LLM beyond endpoint + credential knobs is out of scope.

## Requirements

### Functional Requirements

- **FR-001**: `validate`/`health` MUST report per-service status for all four hard dependencies (qdrant, neo4j, llm, embedding), distinguishing unconfigured / unreachable / auth-failed, each with a remediation message naming the exact knob (BR-12.1.1, NFR-18).
- **FR-002**: `run`/`serve` MUST fail fast (non-zero exit, no successful audit record, no partial-success claim) when any hard dependency is unconfigured or unreachable (BR-12.1.1).
- **FR-003**: Endpoints + credentials for qdrant (`qdrant.url`, `qdrant.api_key`), neo4j (`neo4j.url`, `neo4j.user`, `neo4j.password`), and llm (`llm.endpoint`, `llm.api_key`) MUST be usable from the config layer only (env/.env → kb.local.yml → kb.yml), with values flowing to every client that connects — validate AND ingestion paths, not just health checks (BR-12.2.1).
- **FR-004**: Credential values MUST NOT appear in logs, audit records, error output, or any committed file; example files keep documenting them as placeholders (BR-12.2.2, NFR-13).
- **FR-005**: The package MUST ship a bootstrap script (`scripts/bootstrap-local.sh` or equivalent) that on a clean Docker host: pulls/builds the pinned images declared in `docker-compose.yml` / `Dockerfile` (compose file = single source of image pins), starts the stack, waits for per-service health, and writes machine-local config with the local endpoints (BR-12.3.2, Q11). On a host without a suitable GPU, the bundled llm service MUST be skipped with a warning and external-LLM remediation (config layer or admin UI) — a GPU is not a mandatory condition for bootstrap (owner decision 2026-08-30, BR-12.3.4).
- **FR-006**: The bootstrap MUST be idempotent: re-run against a healthy stack → no re-pull, no re-build, no state reset, per-service status reported, exit 0 (BR-12.3.2).
- **FR-007**: The bootstrap MUST fail with remediation (install/start Docker, free port N) on missing Docker / down daemon / port conflict, without writing partial config (BR-12.3.2).
- **FR-008**: The bootstrap script MUST pass the host-neutrality guard — `tests/integration/test_portability.py` extended to cover `scripts/` (BR-12.3.3, NFR-13).
- **FR-009**: Hosting-mode switch (local ↔ external, per service) MUST be config-only, with identical pipeline behavior (BR-12.4.1).
- **FR-010**: The web admin UI MUST expose endpoint + credential configuration for all four service dependencies (qdrant url/api_key, neo4j url/user/password, llm endpoint/api_key, embedding endpoint where applicable), admin-gated per the feature-003 role model, persisted to the machine-local config layer, and secret-safe (credentials masked on read, never echoed in responses, logs, or audit) — so external or partial hosting is completable from the UI without file edits (owner decision 2026-08-30).

### Key Entities

- **Service dependency**: name (qdrant | neo4j | llm | embedding), endpoint knob, credential knob, hard/optional flag, observed status (unconfigured | unreachable | auth-failed | healthy), remediation message.
- **Bootstrap result**: per-service status, config file written, idempotency flag (ran fresh | no-op), exit status.

## Success Criteria

### Measurable Outcomes

- **SC-001**: 4/4 hard services are individually down-able in test fixtures, with `validate` naming each failing service and `run` exiting non-zero with no successful audit record — 0 silent no-op runs across the suite.
- **SC-002**: A user with fully external services completes init → validate → first run using only documented config knobs or the admin UI; 0 occurrences of any credential value in committed files, captured logs, or audit records in the test evidence.
- **SC-003**: On a clean GPU-capable Docker host, one bootstrap invocation reaches a fully healthy stack and `validate` passes with no user-authored compose/config files (manual proof, per 005 c4); on a CPU-only host, one invocation reaches a healthy partial stack (LLM skipped with documented external-LLM guidance) and exits 0; automated checks assert the script exists, passes the portability guard, and its mocked surface covers pull / skip / fail-fast / no-GPU branches.
- **SC-004**: A bootstrap re-run against a healthy stack performs 0 pulls, 0 builds, 0 state writes, and reports per-service status (automated via the mocked exec surface).

## Assumptions

- **A1**: All four services are hard in v1 (no Qdrant-only / no-graph mode); owner-confirmed by the BR-12 input.
- **A2**: Credential knobs already exist in the config layer (`qdrant.api_key` / `KB_QDRANT__API_KEY`, `neo4j.user` + `neo4j.password` / `KB_NEO4J__USER` + `KB_NEO4J__PASSWORD`, `llm.api_key` / `KB_LLM__API_KEY`); this feature verifies end-to-end wiring + secret hygiene rather than re-designing knobs.
- **A3**: `health.py` already exposes check_qdrant / check_neo4j / check_llm with unconfigured-vs-unreachable distinctions; the delta is (a) embedding coverage, (b) the auth-failed distinction, and (c) enforcing the run/serve fail-fast gate on top of `validate`.
- **A4**: Bootstrap writes machine-local config to `kb.local.yml` (machine-specific, untracked, per BR-11.2.1); local-mode credentials come from `.env` (e.g. `NEO4J_PASSWORD`), as the reference compose already does.
- **A5**: The reference compose's `embedding-model` service (python:3.11-slim, no startup command) is a tracked feature-005 defect, **out of 008 scope** (owner decision 2026-08-30). The bootstrap health-gate surfaces it (no papering over); on hosts where the local embedding service cannot be healthy, embedding falls back to the in-process default model or a user-configured external endpoint — via the config layer or the admin UI — so digital-twins still works.
- **A6**: v1 image distribution stays per feature-005 c1 (user-builds; no registry push) — "pull" covers public reference images (qdrant, neo4j, sglang) plus the local build of `digital-twins` from the in-repo Dockerfile.
- **A7**: A native CLI wrapper (`digital-twins services up`) is a follow-up; v1 ships the script + a documented one-liner in the README quick start.
- **A8**: The live `docker compose up` proof stays manual on a Docker host (005 c4); CI asserts the script's static + mocked behavior.
- **A9**: A suitable GPU is NOT a mandatory condition for bootstrap (owner decision 2026-08-30): on CPU-only hosts the bundled LLM service is skipped and external LLM configuration — via the config layer or the web admin UI — is the supported completion path. BYO-LLM beyond the standard endpoint + credential knobs is out of scope for v1.
- **A10**: Teardown/stop (`--down`) is out of 008 scope (owner decision 2026-08-30): `docker compose down` (volumes preserved) is documented in the script help + README; follow-up ticket if demand shows.
