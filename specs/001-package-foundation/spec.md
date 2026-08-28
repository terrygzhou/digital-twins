# Feature Specification: Portable Package Foundation

**Feature Branch**: `001-package-foundation`

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "BRD slice BR-11.1.1–BR-11.1.5 + BR-11.2 (requirement.md §2): an installable, environment-portable foundation for the digital-twins KB ingestion package — layered config, first-run initializer, named sources with fail-fast enablement, health validation, and zero host coupling."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Clean-host install, init, and health check (Priority: P1)

A community user installs the package on a machine where it has never run before. They
point it at their own vector store, graph store, and LLM, complete the first-run
initializer, and get a working configuration with a clear health report — without touching
any code and without any files existing on the host beforehand.

**Why this priority**: This is the entire value proposition of BR-11.1. Nothing else
(scheduling, multi-user, MCP, web) is usable until a fresh host can install, initialize,
and validate the tool in under half an hour (NFR-12).

**Independent Test**: Install the package on a clean host, run the initializer, run the
health check; success is a passing health report with all sources disabled and zero
host-specific files required.

**Acceptance Scenarios**:

1. **Given** a clean host with no prior state, **When** the user installs the package and runs first-run initialization, **Then** init prompts for (or reads from config) the vector-store, graph-store, and LLM endpoints, creates the local state directory and account database, and writes a starter config in which every source is disabled.
2. **Given** a completed initialization, **When** the user runs the health validation, **Then** the tool reports pass/fail for each configured endpoint with a remediation hint per failure, and no host-specific file is required for any step.
3. **Given** a clean host, **When** the user follows only the quick-start documentation, **Then** install → init → first passing validation completes in under 30 minutes without reading source code.

---

### User Story 2 - Environment decoupling: no host coupling, fail-fast enablement (Priority: P1)

An operator on any host configures their own sources (which agent runtimes they have,
which credentials they hold) purely through configuration. The package never assumes where
anything lives. A source enabled without its prerequisite refuses to run and says exactly
what is missing — it never silently ingests zero items.

**Why this priority**: This is the core of BR-11.2 / NFR-13 and the reason the baseline
cron job could not be shared. Every later slice (sources, scheduler, MCP, web) resolves
locations through this config layer, so it must be right before anything else is built on it.

**Independent Test**: On a configured host, enable a source whose prerequisite is absent;
the first run/validation must fail fast with an error naming the missing prerequisite. A
scan of shipped code, config defaults, and docs must find zero host-specific values.

**Acceptance Scenarios**:

1. **Given** a source enabled while one of its declared prerequisites is missing (e.g. an unreadable session store), **When** a run or validation executes, **Then** it fails fast with a clear error naming the specific missing prerequisite — not a silent zero-item ingest.
2. **Given** the shipped package, **When** code, config defaults, and documentation are audited, **Then** no host-specific path, username, or install location appears anywhere.
3. **Given** a fresh install, **When** the first configuration is written, **Then** every source (built-in and custom) is disabled by default.

---

### User Story 3 - Complete, predictable configuration surface (Priority: P2)

A user discovers every knob the tool exposes through two shipped example files, grouped by
concern (endpoints / embedding / graph / agent-runtimes / email / …), and knows exactly
which layer wins when they set the same value in more than one place.

**Why this priority**: Portability without a complete documented surface is just moving
the host coupling into docs. Required for the NFR-12 "don't read source code" promise, but
usable incrementally after P1 lands.

**Independent Test**: Set the same value in two layers and confirm the documented
precedence order resolves it; audit the shipped examples against the live config surface
for undocumented knobs.

**Acceptance Scenarios**:

1. **Given** the same knob set in an environment variable and in the committed config file, **When** the tool resolves configuration, **Then** the documented precedence (environment → machine-local → committed defaults → built-in defaults) determines the value, deterministically.
2. **Given** the shipped example configuration and environment files, **When** audited against the live configuration surface, **Then** zero knobs exist that are not documented in the examples, grouped by source.

---

### User Story 4 - Safe invariants: embedding mismatch, declared runtime, versioning (Priority: P2)

A user who changes their embedding model or upgrades the package is protected: a
dimension mismatch between the configured model and the existing collection is a loud
hard error with a fix-it message, the supported runtime range is declared rather than
assumed, and the package reports a machine-readable version with a changelog.

**Why this priority**: These are data-safety and upgrade-safety invariants (NFR-2, NFR-15,
BR-11.1.5) that prevent silent corruption; they matter most once real data exists, but
the checks are cheap to build now and hard to retrofit after collections exist.

**Independent Test**: Point the tool at a collection whose vector dimension differs from
the configured model; the run must hard-fail with a remediation message. Run the
version command on two versions and diff the changelog.

**Acceptance Scenarios**:

1. **Given** a configured embedding model whose vector dimension differs from the target collection's existing dimension, **When** the tool runs, **Then** it hard-fails with a remediation message (re-embed, or point at a new collection) — never a silent mismatch.
2. **Given** the installed package, **When** the user queries its version, **Then** a machine-readable version is returned and a changelog is available describing changes between releases.
3. **Given** an in-place upgrade across a version boundary, **When** the new version starts, **Then** the local state, account database, and configuration are preserved, with any schema migration completed before the new code runs.

---

### User Story 5 - Extendable source model without a package update (Priority: P2)

A user whose data lives in a channel the package doesn't ship (their own tool's session
store, a queue, a directory) adds a new source through configuration — capability
declaration, credential, and stamped tag included — without waiting for a release.

**Why this priority**: BR-11.2.7 requires new sources to be addable without a package
update. Defining the source contract now (instead of retrofitting after built-in sources
freeze) is what keeps the channel-extension pattern cheap; the first custom source is the
proof it works.

**Independent Test**: Define a custom source in configuration on a configured host; verify
it participates in a run with its declared capability checks, credential requirement,
and stamped tag, and that it fails fast when its prerequisite is absent.

**Acceptance Scenarios**:

1. **Given** a user-defined source declared in configuration following the channel-extension pattern, **When** the tool runs, **Then** the new source participates with its declared capability checks, credential requirement, and source tag — with no package update.
2. **Given** a user-defined source enabled without its declared credential, **When** a run executes, **Then** it fails fast with an error naming the missing prerequisite.

---

### Edge Cases

- **Conflicting layers**: the same knob set in both an environment variable and the committed config → the documented precedence resolves it deterministically; the tool reports (in debug output) which layer won.
- **Interrupted init**: the user cancels or the host dies mid-initialization → re-running init is safe: it resumes or re-prompts without corrupting the partial state it created.
- **Dimension mismatch after cutover**: user swaps the embedding model after data exists → hard error with remediation (User Story 4), never partial re-embedding.
- **Re-enable after disable**: a source is disabled, its prerequisite disappears, then the source is re-enabled → the fail-fast check runs at enable-time and run-time, not only at init.
- **Foreign state directory**: two installs/state directories on one host → each resolves its own state; no cross-contamination of high-water marks or account data.
- **No network at init time**: init completes for local state and config; endpoint health is reported as unreachable (not an init failure) with a retry hint.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The tool MUST be installable as an installable unit under a stable name (e.g. `digital-twins`) such that installation on a clean host yields a working command-line tool.
- **FR-002**: The package MUST declare all runtime dependencies — supported runtime range, vector-store client, graph-store client, embedding model, and any session-store/email libraries — in a machine-readable manifest. No implicit dependency on files outside the package.
- **FR-003**: The package MUST ship a first-run initializer that: prompts for (or reads from config) the vector-store, graph-store, and LLM endpoints; creates the local state directory and account database; writes a starter config with sane defaults (all sources disabled); runs the health validation; and reports health.
- **FR-004**: The package MUST be self-contained at runtime: no script may live outside the installed package. The host-specific baseline scripts are absorbed into (or reimplemented inside) the package so a fresh install works with zero host-specific files.
- **FR-005**: The package MUST expose a machine-readable version command and ship a changelog so users can track upgrades.
- **FR-006**: All configuration MUST resolve through a four-layer precedence: process environment / env file → machine-local file (untracked) → committed defaults file → built-in defaults. A fresh install MUST start with every source disabled.
- **FR-007**: Every ingestion source MUST be a named, configurable source carrying a capability declaration: required agent-runtime, required credential, and the source tag/prefix it stamps on ingested records.
- **FR-008**: Enabling a source without its declared prerequisite MUST fail fast with a clear error naming the missing prerequisite — never a silent zero-item ingest (at both enable-time and run-time).
- **FR-009**: The package MUST ship an example configuration and an example environment file documenting every configurable knob, grouped by source. No knob may exist that is not documented.
- **FR-010**: The tool MUST detect a mismatch between the configured embedding model and the target collection's existing vector dimension and hard-fail with a remediation message (re-embed, or point at a new collection).
- **FR-011**: Users MUST be able to add new (custom) sources via configuration, following the channel-extension pattern, without a package update.
- **FR-012**: An in-place upgrade MUST preserve the local state directory, account database, and configuration; any schema migration MUST complete before new code starts.
- **FR-013**: No host-specific path, username, or install location MAY appear in shipped code, config defaults, or documentation; all such values resolve through the config layer at runtime.

### Key Entities *(include if feature involves data)*

- **Source**: a named ingestion channel. Attributes: enabled flag, capability declaration (required runtime, required credential, stamped source tag), per-source knobs (e.g. caps). Built-in sources are pre-declared; custom sources are user-defined via configuration.
- **Configuration (layered)**: the resolved view of the four config layers. Contents: endpoint settings (vector store, graph store, LLM), embedding model identity, chunking parameters, source definitions and overrides.
- **Local State**: the host-local state directory and account database created by init. Holds high-water marks and account records; MUST survive upgrades (FR-012).
- **Endpoint**: an external service target (vector store, graph store, LLM) supplied by the user; all endpoints are unset on a fresh install and prompted for during init.
- **Health/Validation Report**: pass/fail per endpoint plus a remediation hint per failure; the output of init's final step and of standalone validation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a clean host with the declared runtime and network access, install → init → first passing validation completes in under 30 minutes without reading source code (NFR-12).
- **SC-002**: 100% of configuration-surface knobs are documented in the shipped example files; an audit finds 0 undocumented knobs.
- **SC-003**: An automated scan of shipped code, config defaults, and documentation finds 0 host-specific paths, usernames, or install locations (NFR-13).
- **SC-004**: A fresh install reports 0 enabled sources; enabling any source with a missing prerequisite produces an error naming the missing prerequisite on 100% of attempts (never a silent zero-item ingest).
- **SC-005**: An embedding-model / collection-dimension mismatch produces a hard error with a remediation message on 100% of attempts; silent mismatch occurrences are 0 (NFR-2).
- **SC-006**: Upgrading across a version boundary preserves the state directory, account database, and configuration with 0 data loss (NFR-15).

## Assumptions

- Users have the declared runtime and network access at install time (NFR-12); bundled containerized deployment (BR-11.1.6) and the publication mechanics (PyPI, BR-11.1.7) are separate release/packaging slices built on this foundation, not part of it.
- Endpoints (vector store, graph store, LLM) are user-supplied external services in this slice; the tool's defaults are all-unset so init prompts for them (BR-11.2.3).
- Multi-user accounts (sign-up, roles, personal tokens — BR-11.4) are out of this slice; init creates the account database so later slices have a home, and first-account-becomes-admin applies when accounts are introduced.
- Scheduling (BR-11.3), MCP scheduler tools (BR-11.5), and the web application surface (BR-11.1.8) consume the config layer and local state defined here but are out of this slice.
- The baseline host pipeline (`~/.hermes/skills/hermes/personal-kb/scripts/`, Hermes cron `e4735cf2a2f2`) is available as implementation reference only; it is a context, never a runtime dependency (BR-11.1.4).
- The machine-local configuration layer is, by convention, untracked/version-ignored so host-specific values never enter the repository.
