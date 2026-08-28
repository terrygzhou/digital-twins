<!--
SYNC IMPACT REPORT
- Version change: unversioned template -> 1.0.0 (initial ratification; MINOR baseline)
- Modified principles: none (first ratification; 5 template slots expanded to 6 concrete principles)
- Added sections: Additional Constraints, Development Workflow, Governance (filled)
- Removed sections: none
- Templates requiring updates:
  - .specify/templates/plan-template.md (reviewed: generic Constitution Check gate, no project refs; no update needed)
  - .specify/templates/spec-template.md (reviewed: generic; no update needed)
  - .specify/templates/tasks-template.md (reviewed: test-first ordering already present; no update needed)
  - .specify/templates/checklist-template.md (reviewed: generic; no update needed)
  - .specify/templates/commands/ (not present; n/a)
- Follow-up TODOs: none
-->

# Digital Twins Constitution

## Core Principles

### I. Portability & Environment Neutrality

Every host-specific value — paths, usernames, install locations, runtime pins — MUST be
resolved through the config layer at runtime and MUST NOT appear in shipped code, config
defaults, or documentation. A fresh install starts with every ingestion source disabled.
Rationale: the package must work on any host unmodified (BR-11.2, NFR-13).

### II. Deterministic, Idempotent Ingestion

Ingestion MUST be idempotent across all trigger paths (schedule, one-shot CLI, API, MCP,
web UI): the same content ingested via any combination of triggers yields exactly one
record (deterministic IDs, deduplication, shared state store). Duplicate records are a
defect, not an edge case (NFR-1, NFR-14).

### III. Test-First (NON-NEGOTIABLE)

Tests MUST be written before the implementation they govern and MUST fail before the
implementation makes them pass (Red-Green-Refactor). The one-record-not-N invariant MUST
have an automated check covering every trigger path (NFR-1, NFR-14).

### IV. Config-First, Fail-Fast

Every behavior-affecting knob MUST exist on the documented config surface (process env /
env file -> machine-local -> committed defaults -> built-in defaults); no knob may exist
that is not documented in the shipped example files. Enabling a source whose prerequisite
is missing MUST fail fast with an error naming the missing prerequisite — never silently
ingest zero items (BR-11.2.2, BR-11.2.4, BR-11.2.7).

### V. Auditability & Observability

Every ingestion run MUST produce an audit record attributable to a user or `system`
regardless of trigger (schedule, manual, api, mcp, ui). Records MUST be queryable by
their originating user; admins may query all. Structured logging is required on scheduler
and ingestion paths (BR-5.3, NFR-16).

### VI. Upgrade Safety & Versioning

In-place upgrades MUST preserve local state (state store, account database, config); any
schema migration MUST complete before new code starts (NFR-15). Releases follow semantic
versioning; a breaking config change requires a major bump plus a migration note
(BR-11.6.3, BR-11.6.4).

## Additional Constraints

- The supported Python range is declared in the package manifest; a host's pinned
  interpreter MUST NOT be an implicit assumption (BR-11.2.6).
- The embedding model and backend versions are pinned. A mismatch between the configured
  embedding model and the collection's existing vector dimension MUST surface as a hard
  error with a remediation message — silent dimension drift is forbidden (NFR-2, BR-11.2.5).
- The package ships under the MIT License (Q4, BR-11.6.2).
- No runtime dependency on files outside the installed package; host scripts the baseline
  pipeline relied on are absorbed into the package (BR-11.1.4).

## Development Workflow

- Work flows through the Spec-Kit pipeline: specify -> clarify -> plan -> tasks ->
  implement. Code MUST NOT be written before a feature's spec and plan exist.
- Every review verifies constitution compliance: portability scan (no host values),
  idempotency check, audit-record shape.
- Complexity requires justification: any deviation from this constitution MUST be recorded
  in the feature's plan (Complexity Tracking) with the named simpler alternative and the
  reason it was rejected.

## Governance

- This constitution supersedes all other development practices in this repository.
- Amendments require a written rationale, a semantic version bump (MAJOR = principle
  removal/redefinition, MINOR = addition/expansion, PATCH = clarification), and a Sync
  Impact Report covering dependent templates.
- Every PR/review verifies compliance with the principles above; violations MUST be
  documented in the feature's plan before merge.

**Version**: 1.0.0 | **Ratified**: 2026-08-29 | **Last Amended**: 2026-08-29
