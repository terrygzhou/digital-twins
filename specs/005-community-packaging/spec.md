# Feature Specification: Community Packaging & Documentation

**Feature Branch**: `005-community-packaging`

**Created**: 2026-08-29

**Status**: Draft (specify stage — clarify/plan/tasks pending)

**Input**: User description: "BRD slice BR-11.6 (requirement.md §2) plus the 001 out-of-scope handoff items (Docker bundle, PyPI publication mechanics): community packaging & documentation — README with 5-minute quick start, MIT LICENSE (Q4), machine-readable issue tracker + semver policy, changelog-driven release notes, stable documented config schema, Docker bundle, PyPI publication."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A stranger ships their first run in 5 minutes (Priority: P1)

The README states what the package is in one paragraph, then a 5-minute quick start (install → init → first run), a configuration reference, and an "add a new source" how-to (BR-11.6.1).

**Why this priority**: this is the community-promise test of the whole BRD — a fresh human, no host context, succeeds without reading source.

**Independent Test**: A user on a clean machine (test harness or real VM) follows only the README and reaches a passing first run.

**Acceptance Scenarios**:

1. **Given** a clean machine, **When** the user follows README quick start verbatim, **Then** `init` completes and a first run passes health validation in <5 minutes.
2. **Given** the configuration reference, **When** a user looks up any shipped knob (incl. 002's `scheduler.*` and 001's full surface), **Then** it is documented with default, type, and precedence (the 001 `test_knob_docs.py` guard is extended to be the automated proof).
3. **Given** the "add a new source" how-to, **When** a user follows it, **Then** they can register a custom entry-point source and see it in `health` (001 US5 path, documented end-to-end).

### User Story 2 - MIT license, machine-verifiable (Priority: P1)

A `LICENSE` file with the MIT text at the repo root (copyright holder = project owner) plus a license declaration in `pyproject.toml`, so pip/PyPI display it correctly (BR-11.6.2, Q4: DECIDED MIT).

**Why this priority**: legal precondition for community trust and PyPI publishing.

**Independent Test**: Both files present; `pip show digital-twins` (from the built wheel) reports License: MIT.

**Acceptance Scenarios**:

1. **Given** the built wheel, **When** it is installed, **Then** `pip show` displays MIT.
2. **Given** the repo, **When** a license detector runs (e.g. `licensecheck`/GitHub license API), **Then** it identifies MIT at the root.

### User Story 3 - Versioning policy + tracker surface (Priority: P2)

A machine-readable issue/feature tracker (GitHub issues or equivalent) and a documented semver policy: breaking config changes require a major bump + migration note (BR-11.6.3).

**Why this priority**: upgrade trust — users must be able to predict what a bump means.

**Independent Test**: The policy document exists, names the rules (config breaking = major), and at least one real release demonstrates compliance.

**Acceptance Scenarios**:

1. **Given** the repo, **When** a new issue/feature is filed, **Then** it lands in the machine-readable tracker (labeling/conventions documented).
2. **Given** a config-breaking change, **When** it is released, **Then** the version is major +1 and the release notes carry a migration note (checked against the changelog in SC-004).

### User Story 4 - Changelog-driven releases + stable config schema (Priority: P2)

Changelog-driven release notes and a stable, documented config schema, so a user upgrading v1 → v2 knows exactly what changed and how to migrate (BR-11.6.4).

**Why this priority**: complements US3 into the upgrade contract.

**Independent Test**: Every release since 0.1.0 has a changelog entry; the config-schema doc enumerates 100% of shipped knobs (automated via the extended knob-docs guard).

**Acceptance Scenarios**:

1. **Given** a new release, **When** it is cut, **Then** `CHANGELOG.md` gains an entry (Added/Changed/Deprecated/Migrated) — enforced by a release checklist/CI check.
2. **Given** the config-schema doc, **When** compared to the runtime schema, **Then** no knob is missing and no undocumented knob exists (the 001 `test_knob_docs.py` guard becomes this check).
3. **Given** a deprecated knob, **When** it is read, **Then** the user gets a one-run deprecation warning naming the replacement (policy demonstrated by at least one fixture in tests).

### User Story 5 - Docker bundle: one command to a running stack (Priority: P2)

The package ships a Docker/Compose bundle so a community user gets Qdrant + Neo4j + LLM endpoint + `digital-twins serve` with one command; the bundle is the reference deployment for BR-11.3.1's "Docker container" target.

**Why this priority**: the lowest-friction path for "any user" (001 out-of-scope handoff).

**Independent Test**: On a machine with Docker only: `docker compose up` yields a healthy stack; `digital-twins health` passes from inside.

**Acceptance Scenarios**:

1. **Given** a Docker host, **When** the bundle is started, **Then** Qdrant/Neo4j/LLM endpoints are reachable from the `digital-twins` service and `health` passes.
2. **Given** the bundle, **When** the user customizes only the compose env/config mount, **Then** no image rebuild is needed (config-in, image-out).

### User Story 6 - PyPI publication path (Priority: P3)

The publication mechanics: build (hatchling, 001), artifact naming, `digital-twins` PyPI project (free name confirmed at 001), trusted-publishing/credentials, and a documented release runbook so publishing is repeatable by any maintainer.

**Why this priority**: closes the last out-of-scope item from 001; no feature value until it exists, but no user value until US1–US5 do.

**Independent Test**: Following the runbook on a throwaway PyPI (TestPyPI), a fresh wheel publishes and installs with MIT license displayed.

**Acceptance Scenarios**:

1. **Given** a release commit, **When** the runbook is followed, **Then** a wheel publishes to TestPyPI and `pip install` from TestPyPI works.
2. **Given** the runbook, **When** a second maintainer follows it cold, **Then** they succeed without tribal knowledge (runbook reviewed against this spec's scenarios).

## Requirements *(mandatory)*

### Functional Requirements

- FR-1 (BR-11.6.1): README = one-paragraph what + 5-minute quick start + config reference + add-a-source how-to.
- FR-2 (BR-11.6.2): MIT `LICENSE` at root + `pyproject.toml` declaration.
- FR-3 (BR-11.6.3): machine-readable tracker surface + documented semver policy (config-breaking = major + migration note).
- FR-4 (BR-11.6.4): changelog-driven release notes + stable documented config schema + deprecation warning mechanism.
- FR-5 (001 handoff): Docker/Compose bundle as the reference `serve` deployment.
- FR-6 (001 handoff): PyPI publication mechanics + runbook.

### Key Entities

- **Release** — version tag + changelog entry + artifacts (wheel) + (optionally) Docker image tag.
- **Config schema doc** — the single source for every knob (guarded automatically).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- SC-001: Fresh-machine README run: clean install → first passing run in <5 minutes (manual or scripted check).
- SC-002: `pip show digital-twins` from a built wheel reports MIT.
- SC-003: 100% of shipped knobs appear in the config-schema doc (automated guard).
- SC-004: Every release since 0.1.0 has a changelog entry; the semver rule is demonstrated by at least one real bump.
- SC-005: `docker compose up` bundle passes `digital-twins health` (SC-005 of US5).
- SC-006: TestPyPI round-trip per the runbook succeeds from a clean environment.

## Assumptions

- A1: The project will live on GitHub (machine-readable tracker = GitHub issues); "or equivalent" leaves the door open, docs target GitHub.
- A2: Docker bundle uses existing official images for Qdrant/Neo4j; the LLM endpoint is user-supplied (env var), not bundled — keeps the bundle small and host-neutral.
- A3: PyPI = production target; TestPyPI is the automated proof environment (SC-006).
- A4: 003's web UI and 004's MCP tools are referenced by the docs, so 005 sequences after them for doc completeness.

## Clarifications

Q4 (MIT) is locked. Open for clarify: (c1) Docker registry choice for the `digital-twins` image itself (recommendation: user-builds-from-repo in v1, no pushed image); (c2) whether the config reference lives in README or `docs/configuration.md` with README linking (recommendation: separate doc, README links — README stays 5-minute-sized).
