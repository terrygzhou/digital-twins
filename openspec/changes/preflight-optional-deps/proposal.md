# Change: Allow Qdrant-only mode without Neo4j credentials

## Why
UAT found that when `neo4j.user` / `neo4j.password` are unset, `preflight`
(`digital_twins/health.py:323`) treats Neo4j as a hard dependency and
fail-fasts with `ServiceDependencyError("neo4j", "unconfigured")`, even in
"Qdrant-only" mode where the user intentionally has no Neo4j backend. This
prevents a valid Qdrant-only ingestion from completing.

This change relaxes the preflight gate so that an unconfigured Neo4j
endpoint is treated as a soft dependency, allowing Qdrant-only ingestion
to proceed.

## What changes
- In `digital_twins/health.py`, introduce a `SOFT_DEPS` set
  (`{"neo4j"}`) and update `preflight` so that a `status="unconfigured"`
  result for a soft dep does NOT raise `ServiceDependencyError`; the
  service is logged and skipped.
- `auth-failed` / `unreachable` / `ok` statuses for a soft dep still
  follow the existing hard/soft behavior: `auth-failed` and
  `unreachable` remain hard-failure (the user explicitly configured the
  service, so a broken config is a real error).
- Update the pipeline's graph-write fallback: when `neo4j_driver` is
  `None` (unconfigured), the pipeline SHALL skip graph writes and log a
  `INFO` line "Qdrant-only mode: Neo4j not configured — skipping graph
  writes".
- Update `run_pipeline` to tolerate `neo4j_driver is None` without
  raising.

## Non-goals
- No change to `check_neo4j` itself (it still returns
  `status="unconfigured"` when creds are missing).
- No change to `check_qdrant` / `check_llm` / `check_embedding` (they
  remain hard deps).
- No change to the S4 graph schema or point-ID algorithm.
- No change to MCP / CLI / web surfaces' error messages beyond the new
  `INFO` log line.

## Impact
- Affected code: `digital_twins/health.py` (`preflight`),
  `digital_twins/ingest/pipeline.py` (graph-write fallback),
  `digital_twins/scheduler/loop.py` (driver factory already returns a
  `None`-safe path — verify).
- Unblocks NFR-12 portability for Qdrant-only users.
- No new dependencies.
