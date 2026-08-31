# Data Model — 009 Web Admin Service Configuration & Health Panel

No new persistent entities. No state-DB migration. Everything below is transient, per-request view/response data.

## ServiceView (existing, 008 — GET/POST `/api/config/services`)

Per service `qdrant|neo4j|llm|embedding`:
- `url`: string | null — the effective merged value of the URL knob (`qdrant.url`, `neo4j.url`, `llm.endpoint`, `embedding.endpoint` — the endpoint knobs surface under the UI key `url`).
- `*_set`: bool — `api_key_set` (qdrant/llm/embedding), `user_set` + `password_set` (neo4j). Booleans only; credential values never surface.
- Top-level `env_overrides`: string[] — `KB_*` env var names currently shadowing service knobs (empty when none).

Validation: built from `config.loader.load` (four-layer precedence) + `config.schema.env_var_for`; POST returns the post-write view.

## ProbeRequest (new — POST `/api/config/services/probe`)

- `services` (optional): string[] — subset of `["qdrant", "neo4j", "llm", "embedding"]`; absent/omitted → all four.
- Validation: body must be a JSON object; when present, `services` must be a non-empty list of known service names — else 404 `unknown service "<name>"` (non-object body → 400).

## ProbeResult (new — 200 response)

- `services`: object keyed by the requested service names (request order; all four → canonical order `qdrant, neo4j, llm, embedding`).
- Per service:
  - `status`: one of `health.VALID_STATUSES` = `ok | unconfigured | unreachable | auth-failed` (plus the synthesized `unreachable` on the 4.5 s per-service deadline).
  - `detail`: string — from `HealthResult.detail` (scrubbed; no credential values) or the timeout line.
  - `remediation`: string — from `HealthResult.remediation` (knob + env form) or the timeout variant.

Lifecycle: computed per request, never persisted, never logged in full (log lines carry service + status only).

## UI row model (client-side, index.html)

Per service row: `{name, urlInput, credBadges[], envOverride (var name | null), status (pill), remediation, inFlight (bool), saving (bool)}`.

Mapping tables (client-side):
- Display→knob for save: `qdrant→url`, `neo4j→url`, `llm→endpoint`, `embedding→endpoint`.
- Env-var→service for the override indicator: `KB_QDRANT__*→qdrant`, `KB_NEO4J__*→neo4j`, `KB_LLM__*→llm`, `KB_EMBEDDING__*→embedding`.
