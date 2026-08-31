# Web API Contract: 006 Web App

**Feature**: `specs/006-web-app` · **Surface**: the token-authenticated REST
surface under `/api/*` that the static UI and any external client hit.

**Auth model**: every `/api/*` endpoint except the three `/api/auth/*`
endpoints requires a valid session token, supplied as
`Authorization: Bearer <token>` (a `?token=` query fallback is accepted). The
token is verified with `digital_twins.auth.verify_session` (the same helper
003's `auth_checker` uses); absent / expired / revoked → `401`. Endpoints that
require the `trigger_run` capability additionally check the caller's role via
`digital_twins.accounts.require_capability` (003 R3 `ROLE_CAPS`); a reader gets
`403 {"code":"permission_denied","message":"..."}`.

All responses are JSON. Errors use a consistent shape:
`{"error": "<short message>"}` or, for the capability gate,
`{"code":"permission_denied","message":"..."}`.

## Public endpoints (no session token required)

### `GET /` — the static UI

Serves `digital_twins/web/static/index.html` (the single static page).
No auth. Content-Type `text/html`.

### `GET /static/<asset>` — static assets

Serves `digital_twins/web/static/<asset>` (e.g. `style.css`). No auth.
Path-traversal-safe (rejects `..` / absolute paths).

### `POST /api/auth/signup`

Request: `{"email": "<email>", "password": "<password>"}`
- `200` `{"email","role","created":true}` — first account → `role:"admin"`,
  else `role:"reader"` (003 R7/C-4, shared with CLI `init`/`signup`).
- `409` `{"error":"account already exists"}` — duplicate email.
- `400` — missing/blank email or password.

### `POST /api/auth/signin`

Request: `{"email","password"}`
- `200` `{"session_token","expires_at"}` — plaintext token returned once;
  pbkdf2 hash stored in `sessions`; `accounts.last_active` set.
- `401` — unknown email or wrong password.

### `POST /api/auth/signout`

Auth: session token. No body.
- `200` `{"revoked":true}` — the session is revoked; the token stops
  authenticating any `/api/*` request.
- `401` — no/invalid token (nothing to revoke; fail-closed).

## Authenticated endpoints (session token required)

### `GET /api/me`

- `200` `{"email","role","point_count"}` — the caller's email, role, and the
  point count in their owner scope (0 when empty).
- `401` — no/invalid/expired/revoked token.

### `GET /api/kb/points?source=&limit=`

Query: optional `source` (a source name; restricts count + sample to that
source) and optional `limit` (default 5, cap 100; sample row count).
- `200` `{"count": int, "owner_count": int, "source_count"?: int,
  "sample": [{"source","source_url","chunk_index","text"}]}`.
  `count` = collection-wide point count; `owner_count` = points matching the
  caller's `owner_tag`; `sample` = up to `limit` rows (most recent first).
- `401` — no/invalid token.
- `502`/`503` `{"error":"qdrant unavailable: <hint>"}` — Qdrant unreachable.

### `POST /api/kb/search`

Request: `{"query": "<text>", "limit"?: int}` (default limit 5, cap 100).
- `200` `{"results": [{"score","source_url","text","source","chunk_index"}]}`
  — top-N by descending `score`, scoped to the caller's `owner_tag`.
- `400` `{"error":"query must be a non-empty string"}` — blank/missing query.
- `401` — no/invalid token.
- `502`/`503` `{"error":"qdrant unavailable: <hint>"}` — Qdrant unreachable.
- `503` `{"error":"embedding unavailable: <hint>"}` — the config-pinned embedding
  model failed to load or encode the query (distinct from the Qdrant hint;
  names `embedding.model` / `embedding.device`).

### `POST /api/ingest/run`

Request: `{"source": "<name>"}` (a single enabled source) or
`{"source": "all"}` (run all enabled sources) or `{}` (run all enabled).
- `200` run summary `{"run_id","status","counts","points"}` — from
  `digital_twins.ingest.pipeline.run_pipeline`; the `audit_runs` row is
  written with `trigger="web"` and `scheduled_by=<caller-email>`; points are
  owner-stamped with the caller's `owner_tag` (003 R6) and dedup'd
  (NFR-1/NFR-14).
- `403` `{"code":"permission_denied","message":"role 'reader' may not trigger
  a run (capability 'trigger_run')"}` — caller lacks `trigger_run`.
- `400` `{"error":"source '<name>' is not enabled"}` — source not enabled in
  config.
- `400` `{"error":"unknown source '<name>'"}` — source not a known source
  (mirrors `run --once` `UnknownSourceError`).
- `400` `{"error":"no sources enabled"}` — `all`/empty and none enabled.
- `409`/`500` `{"error":"source '<name>': missing prerequisite(s): <list>"}` —
  enabled source missing prerequisites (fail-fast, constitution IV); a
  `failed` audit row is written.
- `401` — no/invalid token.

### `GET /api/audit/recent?limit=`

Query: optional `limit` (default 10, cap 100).
- `200` `{"rows": [{"run_id","started_at","completed_at","status","trigger",
  "scheduled_by","per_source_counts"}]}` — non-admin: only rows where
  `scheduled_by=<caller>`; admin: all rows (NFR-16). Most recent first.
- `401` — no/invalid token.

### `POST /api/kb/chat`

Request: `{"query": "<text>"}`.
- `501` `{"code":"not_implemented","remediation":"set llm.endpoint / llm.model
  to enable chat (006 ships the surface only; R8)"}` — no LLM endpoint
  configured. The endpoint is auth-checked and owner-scoped; generation is a
  follow-up slice.
- `400` `{"error":"query must be a non-empty string"}` — blank query.
- `401` — no/invalid token.

## Status-code summary

| Code | Meaning |
|---|---|
| `200` | success (auth ok, capability ok). |
| `400` | bad/missing input (blank query, source not enabled, unknown source, no sources enabled). |
| `401` | no/invalid/expired/revoked session token (fail-closed). |
| `403` | caller lacks the required capability (`trigger_run`) — reader. |
| `409` | duplicate email (signup) / enabled source missing prerequisites. |
| `501` | chat surface, no LLM endpoint (not_implemented). |
| `502`/`503` | Qdrant unreachable (count/search). |
| `503` | Embedding unavailable (search: model load/encode failure). |

## Notes / parity

- The web trigger goes through `digital_twins.ingest.pipeline` (R3) — the
  **same** code path as `run --once` and MCP `kb_ingest`; the audit row's
  `trigger` is `web` (mirroring 004's MCP `trigger=mcp`).
- The static UI (`/`) and any external client hit this **same** `/api/*`
  surface (SC-006) — there is no parallel client-side logic.
- No host path / username / install location appears in any endpoint, error
  message, or header (NFR-13); the portability guard covers the static
  assets (C-7).
