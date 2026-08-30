# Contract: Web Admin UI — Service Configuration (008, FR-010)

Extends the feature-003 bearer-gated `/api/*` surface. Both routes require role `admin` (resolved via the existing `accounts.get_role(db, caller_email)`; non-admin → 403). Credential values are **never returned** — only a set/not-set flag (FR-004).

## GET /api/config/services

**200**:
```json
{
  "services": {
    "qdrant":    {"url": "http://qdrant:6333", "api_key_set": false},
    "neo4j":     {"url": "bolt://neo4j:7687", "user_set": true, "password_set": true},
    "llm":       {"url": "http://llm:8000/v1", "api_key_set": false},
    "embedding": {"url": null, "api_key_set": false}
  },
  "env_overrides": ["KB_LLM__ENDPOINT"]
}
```
- `url` = the *effective* merged value (env > kb.local.yml > kb.yml > default).
- `env_overrides` = env var names currently shadowing a service value (empty when none).

**Errors**: 401 missing/invalid bearer · 403 role ≠ admin.

## POST /api/config/services

**Request** (any subset of services/keys):
```json
{"llm": {"url": "https://api.example.com/v1", "api_key": "sk-..."}}
```
**200** → same shape as GET (post-write effective view; the submitted key is not echoed, only `api_key_set: true`).
**Persistence**: `config/local_io.merge_write` → `kb.local.yml` (machine-local, gitignored). Atomic; unrelated keys preserved.

**Errors**:
- 401 / 403 — as GET.
- 404 `unknown service "x"` — service not one of qdrant/neo4j/llm/embedding/chunking (chunking is the one non-service section the admin UI may write; see `test_post_config_services_schema_invalid_422` for a chunking write).
- 422 `schema violation: <key>` — value fails config-layer validation (same validator as `load()`).
- 409 `existing kb.local.yml is not parseable YAML` — no write performed.

## Security invariants

- The API layer must not log request bodies containing `api_key`/`password` values (log the masked view only).
- `kb.local.yml` is the only write target; no code path writes `kb.yml` or env from the UI.
