# Capability: health — preflight dependency classification

## MODIFIED Requirements

### Requirement: Preflight classifies service dependencies as hard or soft
`digital_twins/health.preflight` SHALL treat `qdrant`, `llm`, and `embedding`
as hard dependencies: a non-`ok` `HealthResult` for any of these SHALL raise
`ServiceDependencyError` before the pipeline touches any store.

`neo4j` SHALL be a soft dependency:
- `status="ok"` → added to the ok list, pipeline proceeds.
- `status="unconfigured"` → logged at `INFO` ("Neo4j not configured —
  Qdrant-only mode") and skipped; the pipeline SHALL proceed and skip
  graph writes.
- `status="auth-failed"` or `status="unreachable"` → hard-failure
  (`ServiceDependencyError`), because the user explicitly configured the
  service and a broken config is a real error.

#### Scenario: Qdrant-only user completes ingestion without Neo4j
- **WHEN** `neo4j.url` / `neo4j.user` / `neo4j.password` are unset and
  `qdrant` is healthy
- **THEN** `preflight` returns without raising, the pipeline skips graph
  writes, and the Qdrant write completes.

#### Scenario: Configured-but-broken Neo4j still fails fast
- **WHEN** `neo4j.url` is set but the endpoint is unreachable
- **THEN** `preflight` raises `ServiceDependencyError` with a
  remediation message, and the pipeline does not write to any store.

## ADDED Requirements

### Requirement: Preflight log line for soft-dep skip
When a soft dep is skipped due to `unconfigured`, `preflight` SHALL log an
`INFO` line naming the service so operators can confirm Qdrant-only mode
in the server log.

#### Scenario: Server log confirms Qdrant-only mode
- **WHEN** `preflight` skips an unconfigured `neo4j`
- **THEN** the server log contains an `INFO` line such as
  "preflight: service neo4j is unconfigured — skipping (Qdrant-only mode)".

## REMOVED Requirements
(none)
