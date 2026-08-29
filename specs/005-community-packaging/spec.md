# Feature Specification: Community Packaging & Documentation

**Feature Branch**: `005-community-packaging`

**Created**: 2026-08-29

**Status**: specify+clarify+plan complete

**Input**: User description: "BRD slice BR-11.6 (requirement.md §2) plus the 001 out-of-scope handoff items (Docker bundle, PyPI publication mechanics): community packaging & documentation — README with 5-minute quick start, MIT LICENSE (Q4), machine-readable issue tracker + semver policy, changelog-driven release notes, stable documented config schema, Docker bundle, PyPI publication."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A stranger ships their first run in 5 minutes (Priority: P1)

The README states what the package is in one paragraph, then a 5-minute quick start (install → init → first run), a configuration reference, and an "add a new source" how-to (BR-11.6.1).

**Why this priority**: this is the community-promise test of the whole BRD — a fresh human, no host context, succeeds without reading source.

**Independent Test**: A user on a clean machine (test harness or real VM) follows only the README and reaches a passing first run.

**Acceptance Scenarios**:

1. **Given** a clean machine, **When** the user follows README quick start verbatim, **Then** `init` completes and a first run passes health validation in <5 minutes.
2. **Given** the configuration reference (`docs/configuration.md`), **When** a user looks up any shipped knob (incl. 002's `scheduler.*` and 001's full surface), **Then** it is documented with default, type, and precedence (the 001 `test_knob_docs.py` guard is extended to be the automated proof; README stays 5-minute-sized and links to the doc).
3. **Given** the "add a new source" how-to, **When** a user follows it, **Then** they can register a custom entry-point source and see it in `health` (001 US5 path, documented end-to-end).

### User Story 2 - MIT license, machine-verifiable (Priority: P1)

A `LICENSE` file with the MIT text at the repo root (copyright holder = project owner) plus a license declaration in `pyproject.toml`, so pip/PyPI display it correctly (BR-11.6.2, Q4: DECIDED MIT).

**Why this priority**: legal precondition for community trust and PyPI publishing.

**Independent Test**: Both files present; `pip show digital-twins` (from the built wheel) reports License: MIT.

**Acceptance Scenarios**:

1. **Given** the built wheel, **When** it is installed, **Then** `pip show` displays MIT.
2. **Given** the repo, **When** a license detector runs (e.g. `licensecheck`/GitHub license API), **Then** it identifies MIT at the root.

### User Story 3 - Versioning policy + tracker surface (Priority: P2)

A GitHub-flavored, in-repo issue/feature tracker (no live remote required) and a documented semver policy: breaking config changes require a major bump + migration note (BR-11.6.3). Because this repo has no GitHub remote, the "machine-readable tracker" is the in-repo `.github/ISSUE_TEMPLATE` (bug + feature + config-breaking-change templates) plus a machine-readable issues conventions file; the policy lives in `docs/semver-policy.md`.

**Why this priority**: upgrade trust — users must be able to predict what a bump means.

**Independent Test**: The in-repo template + conventions files exist; `docs/semver-policy.md` names the rules (config breaking = major), and at least one real release demonstrates compliance.

**Acceptance Scenarios**:

1. **Given** the repo, **When** a new issue/feature is filed, **Then** it uses a documented in-repo template with labeling/conventions (no live GitHub dependency required).
2. **Given** a config-breaking change, **When** it is released, **Then** the version is major +1 and the release notes carry a migration note (checked against the changelog in SC-004).

### User Story 4 - Changelog-driven releases + stable config schema (Priority: P2)

Changelog-driven release notes and a stable, documented config schema, so a user upgrading v1 → v2 knows exactly what changed and how to migrate (BR-11.6.4). The deprecation mechanism is a one-run warning naming the replacement; it is a **required** deliverable, demonstrated by at least one test fixture.

**Why this priority**: complements US3 into the upgrade contract.

**Independent Test**: Every release since 0.1.0 has a changelog entry; the config-schema doc (`docs/configuration.md`) enumerates 100% of shipped knobs (automated via the extended `test_knob_docs.py` guard).

**Acceptance Scenarios**:

1. **Given** a new release, **When** it is cut, **Then** `CHANGELOG.md` gains an entry (Added/Changed/Deprecated/Migrated) — enforced by a release checklist/CI check.
2. **Given** the config-schema doc, **When** compared to the runtime schema, **Then** no knob is missing and no undocumented knob exists (the 001 `test_knob_docs.py` guard becomes this check; the guard is EXTENDED to assert 100% knob coverage of `docs/configuration.md`).
3. **Given** a deprecated knob, **When** it is read, **Then** the user gets a one-run deprecation warning naming the replacement (REQUIRED mechanism, demonstrated by at least one test fixture).

### User Story 5 - Docker bundle: one command to a running stack (Priority: P2)

The package ships a Docker/Compose bundle so a community user gets Qdrant + Neo4j + LLM endpoint + `digital-twins serve` with one command; the bundle is the reference deployment for BR-11.3.1's "Docker container" target. In v1 the user builds the image from the repo — there is no pushed/pre-built image, so the bundle is a *shipped reference deployment* (files in-repo), not a published artifact.

**Why this priority**: the lowest-friction path for "any user" (001 out-of-scope handoff).

**Independent Test (automated)**: The compose file validates as well-formed YAML with the required services (`qdrant`, `neo4j`, `digital-twins`), config-in mounts, and no host-path / host-pin leakage — the portability guard extended to the compose file. (The live `docker compose up` is a MANUAL proof requiring a Docker host.)

**Acceptance Scenarios**:

1. **Given** a Docker host *(manual)*, **When** the bundle is started, **Then** Qdrant/Neo4j/LLM endpoints are reachable from the `digital-twins` service and `health` passes.
2. **Given** the bundle, **When** the user customizes only the compose env/config mount, **Then** no image rebuild is needed (config-in, image-out).
3. **Given** the in-suite portability guard, **When** it scans `docker-compose.yml`, **Then** no host path, `python3.N` pin, literal username, or `~/.` absolute path appears (extends `tests/integration/test_portability.py` to the compose file).

### User Story 6 - PyPI publication path (Priority: P3)

The publication mechanics: build (hatchling, 001), artifact naming, `digital-twins` PyPI project (free name confirmed at 001), trusted-publishing/credentials, and a documented release runbook so publishing is repeatable by any maintainer.

**Why this priority**: closes the last out-of-scope item from 001; no feature value until it exists, but no user value until US1–US5 do.

**Independent Test (automated)**: The build produces a valid wheel (`python -m build` in a temp venv) and `pip show` from the built wheel reports `License: MIT`; `docs/release-runbook.md` exists. (The live TestPyPI publish is a documented MANUAL runbook step needing network + credentials, not an in-suite test.)

**Acceptance Scenarios**:

1. **Given** a release commit, **When** the in-suite build check runs, **Then** a valid wheel builds and `pip show` from it reports MIT.
2. **Given** a release commit *(manual)*, **When** the runbook is followed, **Then** a wheel publishes to TestPyPI and `pip install` from TestPyPI works.
3. **Given** the runbook, **When** a second maintainer follows it cold, **Then** they succeed without tribal knowledge (runbook reviewed against this spec's scenarios).

## Requirements *(mandatory)*

### Functional Requirements

- FR-1 (BR-11.6.1): README = one-paragraph what + 5-minute quick start + link to `docs/configuration.md` + add-a-source how-to. The full config reference lives in `docs/configuration.md` (new); the README stays 5-minute-sized and links to it.
- FR-2 (BR-11.6.2): MIT `LICENSE` at root + `pyproject.toml` declaration.
- FR-3 (BR-11.6.3): GitHub-flavored, in-repo issue/feature tracker surface (no remote required: `.github/ISSUE_TEMPLATE` + machine-readable issues conventions file) + documented semver policy in `docs/semver-policy.md` (config-breaking = major + migration note).
- FR-4 (BR-11.6.4): changelog-driven release notes + stable documented config schema (`docs/configuration.md`) + REQUIRED one-run deprecation warning naming the replacement (demonstrated by ≥1 test fixture).
- FR-5 (001 handoff): Docker/Compose bundle as the reference `serve` deployment (v1 = user builds image from repo; no pushed/pre-built image). Automated check = compose schema/consistency + host-neutrality guard (portability test extended to `docker-compose.yml`); live `docker compose up` is manual.
- FR-6 (001 handoff): PyPI publication mechanics + runbook. Automated check = valid wheel build + `pip show` reports MIT + `docs/release-runbook.md` exists; live TestPyPI publish is a manual runbook step.

### Key Entities

- **Release** — version tag + changelog entry + artifacts (wheel) + (optionally) Docker image tag.
- **Config schema doc** — `docs/configuration.md`, the single source for every shipped knob (guarded automatically by the extended `test_knob_docs.py`).
- **Compose bundle** — in-repo `docker-compose.yml` reference deployment (qdrant, neo4j, digital-twins services; config-in mounts); user builds the image in v1, no pushed image.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- SC-001: Fresh-machine README run: clean install → first passing run in <5 minutes (manual or scripted check).
- SC-002: `pip show digital-twins` from a built wheel reports MIT (automated: build wheel in temp venv + `pip show`).
- SC-003: 100% of shipped knobs appear in `docs/configuration.md` (automated guard: `test_knob_docs.py` extended to assert full coverage).
- SC-004: Every release since 0.1.0 has a changelog entry; the semver rule is demonstrated by at least one real bump.
- SC-005: **Automated** — `docker-compose.yml` is well-formed YAML with the required services (qdrant, neo4j, digital-twins, llm, embedding-model), config-in mounts, pinned image versions (no `:latest` floats), and no host-path/host-pin leakage (portability guard extended to the compose file). **Manual** — `docker compose up` yields a healthy stack (Qdrant + Neo4j + LLM + embedding model + `digital-twins serve`) and `digital-twins health` passes from inside (requires a Docker host).
- SC-006: **Automated** — the build produces a valid wheel and `pip show` from it reports MIT, and `docs/release-runbook.md` exists. **Manual** — TestPyPI round-trip per the runbook succeeds from a clean environment (requires network + credentials).

## Assumptions

- A1: GitHub-flavored, in-repo, no remote required — the "machine-readable issue/feature tracker" is the in-repo `.github/ISSUE_TEMPLATE` (bug + feature + config-breaking-change) plus a machine-readable issues conventions file; there is no live GitHub dependency. (This repo has no GitHub remote.)
- A2: Docker bundle uses existing official images for Qdrant/Neo4j; per **BR-11.1.6 / Q7 (DECIDED)** it **bundles** the LLM runtime (SGLang or equivalent) and the embedding model (BGE-small-en-v1.5) as the default, with pinned versions (no `:latest` floats), so a user with no external LLM runs end-to-end from the container. External endpoints (Qdrant/Neo4j/LLM/embedding) are an opt-in override via env vars (e.g. `QDRANT_HOST=host.docker.internal`), documented alongside the bundled default in the README. v1 = user builds the image from the repo; NO pushed/pre-built image (the bundle is a shipped reference deployment, not a published artifact). (Resolves former Deferred item D-1: BR-11.1.6's bundle requirement supersedes the earlier user-supplied-LLM assumption.)
- A3: PyPI = production target. The live TestPyPI publish is a MANUAL runbook step (needs network + credentials); the automated in-suite proof is a valid wheel build + `pip show` reports MIT (SC-006).
- A4: 003's web UI and 004's MCP tools are referenced by the docs, so 005 sequences after them for doc completeness.
- A5: Host-neutrality (CRITICAL) — every artifact in 005 (README, docs, Docker/Compose, pyproject) MUST pass `tests/integration/test_portability.py` + `tests/unit/test_knob_docs.py`: no host paths, no `python3.N` pins, no literal usernames, no `~/.` absolute paths.

## Clarifications

### Session 2026-08-29

Q4 (MIT) is locked. The two open clarifications (c1, c2) plus the host-neutrality posture (c3, c4, c5, c6) are now resolved and locked:

- **c1 — Docker registry / image distribution (LOCKED → v1 user-builds):** v1 ships the Docker/Compose bundle as a *shipped reference deployment* — files in-repo (`docker-compose.yml` + image built from the repo). There is **NO pushed/pre-built image** and no registry target. `docker compose up` against the in-repo bundle is a **manual** proof (needs a Docker host); the automated in-suite check is compose-file schema/consistency + the host-neutrality guard (extends `test_portability.py` to the compose file). A future push/registry decision is deferred (post-005) and not part of this spec.
- **c2 — config reference location (LOCKED → separate doc, README links):** The full configuration reference lives in `docs/configuration.md` (new file). The README stays 5-minute-sized and links to it. The extended `test_knob_docs.py` guard asserts that `docs/configuration.md` enumerates 100% of shipped knobs (default, type, precedence) and is the automated proof for SC-003 / US1-AS2 / US4-AS2.
- **c3 — machine-readable tracker (LOCKED → in-repo, GitHub-flavored, no remote):** This repo has no GitHub remote. The "machine-readable issue/feature tracker" = in-repo `.github/ISSUE_TEMPLATE` (bug + feature + config-breaking-change templates) + a documented semver policy (`docs/semver-policy.md`) + a machine-readable issues conventions file. No live GitHub dependency.
- **c4 — Docker proof / SC-005 (LOCKED → automated compose guard + manual `up`):** The automated in-suite check for US5/SC-005 = validate `docker-compose.yml` is well-formed YAML with the required services (`qdrant`, `neo4j`, `digital-twins`), config-in mounts, and no host-path/host-pin leakage (portability guard extended to the compose file). The live `docker compose up` yielding a healthy stack is a **manual** proof requiring a Docker host.
- **c5 — PyPI proof / SC-006 (LOCKED → automated wheel build + manual TestPyPI):** The automated in-suite check for US6/SC-006 = the build produces a valid wheel (`hatchling` build in a temp venv or `python -m build`) + `pip show` from the built wheel reports `License: MIT` + the release runbook doc exists (`docs/release-runbook.md`). The live TestPyPI round-trip publish is a documented **manual** runbook step (needs network + credentials), not an in-suite test.
- **c6 — host-neutrality guard (LOCKED → CRITICAL, enforced):** Everything in 005 (README, docs, Docker/Compose, pyproject) MUST pass `tests/integration/test_portability.py` + `tests/unit/test_knob_docs.py`: no host paths, no `python3.N` pins, no literal usernames, no `~/.` absolute paths. The config reference enumerates 100% of shipped knobs (SC-003) and `test_knob_docs.py` is EXTENDED to be that automated proof. The deprecation warning (US4/FR-4) is a REQUIRED deliverable: a one-run warning naming the replacement, demonstrated by at least one test fixture.

### Deferred (out of clarify scope — not a ruling, needs owner input)

None. D-1 (LLM bundling) is resolved in this session: BR-11.1.6 / Q7 (DECIDED) requires the Docker bundle to **bundle** the LLM runtime (SGLang or equivalent) + embedding model (BGE-small-en-v1.5) as the default with external endpoints as an opt-in env override. See Assumption A2 and SC-005 (updated service list: qdrant, neo4j, digital-twins, llm, embedding-model).

- **Q (R14, plan-stage: 'health' command) → A (locked):** The spec/SC-005/quickstart reference "`digital-twins health`" but 001–004 ship no standalone `health` subcommand (health checks run inside `init` and `validate`). 005 treats "health passes from inside" as **`validate`** (the shipped equivalent); the quickstart/README use `validate` and state the equivalence. A literal `health` subcommand is a follow-up CLI addition, not a 005 packaging change.

- **Q (R15, plan-stage: license form) → A (locked):** `license = { text = "MIT" }` (PEP 621 legacy form) is kept as-is in `pyproject.toml` — it already satisfies `pip show` reporting MIT (with the existing `LICENSE` file). Migration to the SPDX form (`license = "MIT"`) is a documented follow-up, not a 005 change (005 must not touch the manifest).

- **Q (R16, plan-stage: embedding-model service shape) → A (locked):** 001's embedding knob is an *in-process model id*, not an endpoint. For the container to need no external embedding service, the bundle adds a thin `sentence-transformers` HTTP service (the `embedding-model` compose service) pinned to BGE-small-en-v1.5. This is a *deployment* artifact (compose + Dockerfile), not a config-layer knob; host `digital-twins` code is unchanged.

- **Q (R17, plan-stage: Dockerfile base-image vs portability guard) → A (locked):** A `python:3.11-slim` *container base image* is not a host pin — it matches the package's `requires-python >= 3.11` range. The existing `INTERPRETER_PIN` pattern (`python3\d+`) does NOT match the image-reference form (`python:3.11-slim`), so the Dockerfile passes the portability guard as written. Documented in contracts/documentation.md C-9; the red test is written knowing this exact boundary.
