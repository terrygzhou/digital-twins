# Research: Service Dependencies & Hosting Bootstrap (008)

All NEEDS CLARIFICATION items from plan Technical Context resolved. Decisions, rationale, alternatives.

## R1 — Bootstrap implementation: bash script, not a Python subcommand

- **Decision**: ship `scripts/bootstrap-local.sh` (bash + `docker`/`docker compose` only).
- **Rationale**: BR-12.3.2 names a shipped script; the clean-host story is "Docker present, Python maybe not" — the script must work before `pip install` (it pulls/built images; the app runs inside the `digital-twins` container). A `digital-twins services up` subcommand would require host Python + installed package first (A7 keeps it as a follow-up convenience).
- **Alternatives considered**: Python subcommand (needs host runtime; rejected); no script, README instructions (violates Q11 one-invocation requirement; rejected).

## R2 — GPU detection in bootstrap

- **Decision**: `command -v nvidia-smi && nvidia-smi -L` lists ≥1 GPU → bundled LLM service runs; else skip the `llm` compose service with a warning + external-LLM guidance (BR-12.3.4). Env override `LLM_SERVICE=local|skip` for experts (4 lines).
- **Rationale**: nvidia-smi presence+listing is the cheapest reliable probe; no docker probe container needed.
- **Alternatives**: try-to-start and health-wait (burns up to the full timeout on no-GPU hosts — rejected); `docker run --gpus` probe (heavier, needs nvidia-container-toolkit anyway — rejected).
- **Ceiling (ponytail: named)**: exotic GPUs without nvidia-smi or with broken drivers are misdetected; `LLM_SERVICE` override is the escape hatch.

## R3 — Fail-fast gate placement (FR-002)

- **Decision**: preflight inside `digital_twins.ingest.pipeline.run_pipeline` (top of the function): run the four hard-service checks (via `health`), raise `ServiceDependencyError(service, status, remediation)` on any failure. No audit row written for a preflight failure (the run never started; structured log line records service + remediation, Constitution V).
- **Rationale**: every trigger path routes through `run_pipeline` — CLI run (YES — cli.py run() calls run_pipeline directly), scheduler (`scheduler/loop.py:138`), web (`/api/ingest/run` → `app.py`), MCP (`mcp/dispatch.py:376`). One guard in the shared function beats guards in four callers (root-cause fix).
- **Alternatives**: per-caller gates (4× the code, scheduler easily forgotten — rejected); gate only in `validate` (runs would still silently no-op — rejected, violates BR-12.1.1).

## R4 — Auth-failed distinction (FR-001)

- **Decision**: extend `HealthResult` with optional `status: str = ""` (`ok | unconfigured | unreachable | auth-failed`); keep `ok` bool (backward-compatible, existing tests unaffected). Mapping: qdrant 401/403 → auth-failed (ok=False); neo4j `neo4j.exceptions.AuthError` → auth-failed; llm HTTP 401/403 on `/models` → auth-failed, ok=**False** (today 4xx is reported ok=True "may still need care" — too permissive for a hard-dependency gate; wrong credentials must fail fast per BR-12.1.1).
- **Alternatives**: new exception hierarchy (over-engineered; a field is enough — rejected); keep llm lenient (rejects fail-fast; rejected).

## R5 — Embedding endpoint: new knobs + endpoint-aware embedder

- **Decision**: add `embedding.endpoint` + `embedding.api_key` knobs (schema `known_subs` for `embedding`, `knobs.py` registry, `config.example.yml` + `.env.example` entries — T027 `test_knob_docs.py` requires both directions). New `check_embedding(cfg)`: endpoint set → probe `{endpoint}/v1/models` (urllib, mirrors `check_llm`); unset → in-process check (model known + `model_dimension` available → ok). Endpoint-aware embedding: `load_embedder`/embedder factory gains an OpenAI-compatible `/v1/embeddings` HTTP path (stdlib urllib only — no new dependency); in-process path unchanged and remains default.
- **Why in 008**: the compose file already ships `KB_EMBEDDING__ENDPOINT` into the app container, and the schema **rejects unknown keys** (`schema.py:253-255`) — so that env var is a latent startup defect today; adding the knobs makes it functional. (The embedding-model *service startup command* stays the tracked 005 defect, A5.)
- **Alternatives**: keep in-process-only embedding (BR-12.2.1 explicitly lists "the LLM / embedding endpoint" — rejected); new HTTP client dep (urllib is already the house style in `health.py` — rejected).

## R6 — Machine-local config writer (FR-010 persistence)

- **Decision**: new `digital_twins/config/local_io.py`: `local_config_path()` (reuses the loader's `config_dir / "kb.local.yml"` resolution — single source, no new path logic) + `merge_write(updates: dict)` — YAML read-modify-write, atomic via tmp file + `os.replace`, preserves all unrelated keys (edge case: stale config after mode switch). Refuses writes outside the resolved local path.
- **Rationale**: neither `user_config.set_override` (per-user DB overrides) nor the loader (read-only) writes machine-local config today; FR-010 needs exactly one small writer.
- **Alternatives**: shell out to `kb config set` CLI from the web handler (process-per-write, error-prone — rejected); store service config in the DB (config layer is the constitution-mandated surface, Principle IV — rejected).

## R7 — Admin UI contract (FR-010)

- **Decision**: two new bearer-gated routes on the existing web app (003 surface): `GET /api/config/services` (admin) → per-service `{url, api_key_set: bool}` (credentials masked — only a set/not-set flag, never the value); `POST /api/config/services` (admin) → partial update `{service: {url?, api_key?}}` → schema-validated → `merge_write` → 200 with the updated masked view. Non-admin → 403 (existing `get_role(db, caller)` pattern; role `admin`). 422 for schema-invalid values; 404 unknown service.
- **Rationale**: matches 003's gate/model (`web/app.py` role checks, JSON error shapes); secret-safety (FR-004) enforced by design — the value is written, never read back.
- **Alternatives**: full config-file editor in the UI (scope explosion; rejected — services only, per owner decision); token-level permissioning (role model already distinguishes admin; NFR-17 — sufficient).

## R8 — Portability guard extension (FR-008)

- **Decision**: add `scripts/bootstrap-local.sh` to `SHIPPED_NON_PY` in `tests/integration/test_portability.py` (existing `HOST_PATTERNS` already match `/home/<user>/`, `/Users/<user>/`, baseline host runtime dirs; add a `python3\.\d` pin pattern since the script is shell). No new test module — one list entry + one pattern.
- **Alternatives**: separate guard module (rejected; the standing guard is the designated surface).

## R9 — Bootstrap idempotency + config write

- **Decision**: script flow — detect docker/compose → port-conflict probe (6333/7474/7687/8000/8080) → GPU probe (R2) → `docker compose pull` (public images) + `docker compose build digital-twins` ONLY when images absent or compose pins changed (compose file hash in a state file under the config dir / `.kbstate`-adjacent local dir) → `docker compose up -d <services>` (services list excludes `llm` when skipped) → poll `docker compose ps` health until all healthy or `BOOTSTRAP_TIMEOUT_S` (default 600) → write `kb.local.yml` (local endpoints) **only when the file does not exist**; when it exists and its service endpoints disagree with local defaults, print a diff + warning and leave it (never clobber user config) → print per-service status table + next steps.
- **Rationale**: "no re-pull/re-build on healthy re-run" (FR-006) via image/pin check; "never clobber" matches the stale-config edge case in the spec.
- **Alternatives**: always `pull` (network + minutes on re-run — rejected); overwrite kb.local.yml (data-loss risk — rejected).
