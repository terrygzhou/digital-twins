# Quickstart: Validation Guide — Service Dependencies & Hosting Bootstrap (008)

Runnable validation scenarios proving the feature end-to-end. Implementation tasks live in `tasks.md` (Phase 2); this file only says how to prove the spec.

## Prerequisites

- repo + `.venv` (Python per `pyproject.toml` range); pytest available.
- Docker host (manual proofs only — 005 c4 pattern; CI runs the mocked/static surface).

## 1. Fail-fast gate (FR-001/002, SC-001)

```bash
pytest tests/unit/test_preflight_gate.py tests/unit/test_health_status.py -v
```
Expected: 4/4 services individually down-able; `validate` output names the service with status ∈ {unconfigured, unreachable, auth-failed} + remediation; `run_pipeline` raises `ServiceDependencyError` before any audit row; wrong-credential cases report `auth-failed` and fail.

## 2. External hosting + secret hygiene (FR-003/004, SC-002)

```bash
pytest tests/unit/test_config_credentials.py tests/unit/test_config_local_io.py -v
```
Expected: env-only external endpoints + tokens reach qdrant/neo4j/llm clients in both validate and ingestion paths; no credential value in any captured log/audit/error fixture; `config.example.yml` + `.env.example` document every new knob (T027 `pytest tests/unit/test_knob_docs.py` green).

## 3. Bootstrap static + mocked surface (FR-005..008, SC-003/004)

```bash
pytest tests/unit/test_bootstrap_script.py tests/integration/test_portability.py -v
bash -n scripts/bootstrap-local.sh        # shell syntax
```
Expected: mocked docker/exec surface covers pull / skip-when-present / fail-fast / no-GPU / idempotent-re-run branches; portability guard green with `scripts/bootstrap-local.sh` in the SHIPPED scan.

## 4. Admin UI service config (FR-010, SC-002)

```bash
pytest tests/integration/test_web_config_api.py -v
```
Expected: admin GET/POST per `contracts/web-config-api.md`; non-admin → 403; credentials masked (set-bool only); write persists to `kb.local.yml` atomically and preserves unrelated keys; `env_overrides` reported when an env var shadows a written value.

## 5. Manual Docker proofs (005 c4 — not CI)

1. **GPU host, clean**: `bash scripts/bootstrap-local.sh` → exit 0, `digital-twins validate` healthy 4/4, one `digital-twins run --once` ingests.
2. **CPU-only host**: same → exit 0 with `llm: SKIPPED`, set `KB_LLM__ENDPOINT` (env or `POST /api/config/services`), `validate` healthy 4/4.
3. **Re-run**: second invocation → 0 pulls/builds, per-service status, exit 0.
4. **External mode**: point all four services at external endpoints via the admin UI → `validate` + run pass without the local stack; no credential value in UI responses/logs/audit.
