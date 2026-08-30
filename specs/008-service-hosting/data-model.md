# Data Model: Service Dependencies & Hosting Bootstrap (008)

No database schema changes: `audit_runs` and `accounts` are untouched; service configuration lives in the file-based config layer (Constitution IV). The entities below are runtime/views, not tables.

## ServiceDependency (view — produced by `health.run_health_checks`)

| Field | Type | Notes |
|---|---|---|
| `service` | str | `qdrant` \| `neo4j` \| `llm` \| `embedding` |
| `status` | str | `ok` \| `unconfigured` \| `unreachable` \| `auth-failed` (new `HealthResult.status` field, optional, default empty → treated as ok/unconfigured by legacy consumers) |
| `ok` | bool | unchanged semantics; now `False` for auth-failed on the llm path (R4) |
| `detail` | str | human detail, never contains credential values |
| `remediation` | str | names the exact knob (e.g. "set llm.api_key (env: KB_LLM__API_KEY)") |

**State transitions**: observed fresh on each `validate`/`health`/preflight call; no persisted state. Preflight (R3) consumes the same four results: any `status != ok` → `ServiceDependencyError(service, status, remediation)`.

**Validation rules** (from spec):
- all four services are hard in v1 (A1) — no optional flag;
- `unconfigured` must be distinguishable from `unreachable` (FR-001);
- credential values must never appear in `detail`/`remediation` (FR-004).

## LocalConfigWrite (operation — `config/local_io.py`)

- **Target**: `config_dir / "kb.local.yml"` (loader-resolved; untracked, gitignored).
- **Input**: `updates: dict` — partial mapping of service section keys (`qdrant.url`, `qdrant.api_key`, `neo4j.url`, `neo4j.user`, `neo4j.password`, `llm.endpoint`, `llm.api_key`, `embedding.endpoint`, `embedding.api_key`).
- **Semantics**: read existing file (if any) → deep-merge updates → atomic write (tmp + `os.replace`) → all unrelated keys preserved. Pre-state: file absent or present with arbitrary user keys. Post-state: updated keys set, others byte-identical in meaning. Failure modes: unparseable existing YAML → abort, no write (422 in the API surface); resolved path outside the config dir → refused.
- **Consumers**: web `POST /api/config/services` (FR-010); NOT the bootstrap script (bash writes the initial file via heredoc when absent — R9).

## ConfigServiceView (web resource — see contracts/web-config-api.md)

- **GET view**: `{services: {<svc>: {url: str|null, api_key_set: bool}}}` — credentials reduced to a boolean (never the value).
- **POST input**: `{<svc>: {url?: str, api_key?: str}}` — any subset; schema-validated against the config `known_subs` per service before `merge_write`.
- **Relationships**: view derives from the merged config (env > local > kb.yml > defaults); after POST the *local layer* changes, and env overrides (if any) keep winning — the response includes an `env_overrides` note when an env var shadows a just-written value (prevents the "I set it in the UI but it didn't change" trap).
