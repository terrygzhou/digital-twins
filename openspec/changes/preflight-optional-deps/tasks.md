# Tasks: preflight-optional-deps

## 1. Preflight gate
- [x] 1.1 In `digital_twins/health.py`, add a `SOFT_DEPS = {"neo4j"}` module
      constant and update `preflight` so that a `HealthResult` with
      `status="unconfigured"` for a soft dep does NOT raise
      `ServiceDependencyError` — the service is logged at `INFO` and
      skipped. `auth-failed` and `unreachable` for a soft dep remain
      hard-failure. `ok` is added to `ok_services` as before.
      Test-First: unit test in `tests/unit/test_health.py` (new file if
      needed) that covers all four statuses for `neo4j` and at least one
      hard dep (`qdrant`) to confirm hard-dep behavior is unchanged.
- [x] 1.2 Confirm the `run_pipeline` docstring (L147) is still accurate:
      "`neo4j` is a driver-shaped object exposing `.run(query, **params)`
      (None = skip graph writes)" — add a note that `None` now also means
      "Qdrant-only mode (Neo4j unconfigured)".

## 2. Pipeline graph-write fallback
- [x] 2.1 In `digital_twins/ingest/pipeline.py`, ensure `run_pipeline`
      tolerates `neo4j is None` by skipping `_upsert_graph` /
      `_ensure_graph_schema` / `_run_extraction` with an
      `INFO` log line ("Qdrant-only mode: Neo4j not configured —
      skipping graph writes"). Test-First: unit test that `run_pipeline`
      with `neo4j=None` completes a Qdrant write without raising.

## 3. Standing guards + SDD
- [x] 3.1 Confirm `tests/integration/test_portability.py` (T006) and
      `tests/unit/test_knob_docs.py` (T027) remain green.
- [x] 3.2 Run `PYTHONPATH=. python3.12 -m pytest
      tests/unit/test_health.py tests/integration/test_portability.py -q`.

## Verification
- `PYTHONPATH=. python3.12 -m pytest tests/unit/test_health.py -q`
