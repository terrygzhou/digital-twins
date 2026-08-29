# Data Model: 006 Web App

**Feature**: `specs/006-web-app`

006 introduces **no new table, no new migration, and no new schema column**.
It reuses the 001/002/003 state store as-is. This file documents the entities
the web app *reads and writes* and the (in-memory) request/response shapes the
`/api/*` surface exposes. The point of this artifact is to make the
reuse-vs-new boundary explicit so the plan and tasks are grounded.

## Reused state (unchanged by 006)

These exist already (001 v1/v2 + 003 v3 DDL, `digital_twins/state/models.py`)
and are **not** modified by 006:

| Table | 006 usage |
|---|---|
| `accounts` (id, email UNIQUE, role, password_hash, created_at, last_active) | sign-up/sign-in read role + password_hash; role gates `trigger_run` (003 R3). |
| `sessions` (session_token PK, account_email, created_at, expires_at, revoked) | sign-in creates a session (pbkdf2 hash + 8h TTL); sign-out flips `revoked`; `/api/*` verifies via `auth.verify_session`. |
| `audit_runs` (run_id PK, started_at, completed_at, status, trigger, scheduled_by, per_source_counts) | a web trigger writes a row with `trigger="web"`, `scheduled_by=<caller>`; `/api/audit/recent` reads it. |
| `highwater` (source, item_key, last_key, updated_at) | read by `run_pipeline` to skip unchanged items (dedup parity, NFR-1/NFR-14). |
| `personal_tokens`, `user_config` | not used by the web app in 006 (MCP / CLI surfaces). |

The Qdrant `personal_kb` collection (`digital_twins.health.QDRANT_COLLECTION`)
is read for point counts / samples / search; a web-triggered run writes to it
through `run_pipeline` exactly as `run --once` does.

## New config knobs (the only new persisted state)

Three new knobs, all in the new `web` section. No table; they live in the
config layer (env → kb.local.yml → kb.yml → built-in defaults).

| Knob | Type | Default | Env var | Notes |
|---|---|---|---|---|
| `web.bind` | str | `127.0.0.1` | `KB_WEB__BIND` | Bind address; loopback by default (BR-8.8). Widen to `0.0.0.0` to reach across the network. |
| `web.port` | int | `8767` | `KB_WEB__PORT` | Web app port; distinct from `scheduler.status_port` (8765) and `mcp.port` (8770). |
| `web.base_url` | str | `http://localhost:8767` | `KB_WEB__BASE_URL` | Optional base URL for building absolute/redirect URLs. |

Lock-step requirement: all four surfaces (`digital_twins/config/knobs.py`,
`config.example.yml`, `.env.example`, `docs/configuration.md`) must carry the
three knobs identically, or `tests/unit/test_knob_docs.py` fails
(constitution IV, SC-003).

## In-memory request/response shapes (the `/api/*` contract)

These are not persisted; they are the JSON the web app returns. The full
field-level contract is in `contracts/web-api.md`.

- **`GET /api/me`** → `{ "email": str, "role": "admin"|"scheduler"|"reader",
  "point_count": int }`. Auth: session token.
- **`POST /api/auth/signup`** body `{email, password}` →
  `200 {email, role, created: true}` (first account `admin`, else `reader`)
  / `409 {error: "account already exists"}`.
- **`POST /api/auth/signin`** body `{email, password}` →
  `200 {session_token, expires_at}` / `401`.
- **`POST /api/auth/signout`** (auth: session token) →
  `200 {revoked: true}`; the token stops authenticating `/api/*`.
- **`GET /api/kb/points?source=&limit=`** (auth) →
  `{count, owner_count, source_count?, sample: [{source, source_url,
  chunk_index, text}]}`.
- **`POST /api/kb/search`** body `{query, limit}` (auth) →
  `{results: [{score, source_url, text, source, chunk_index}]}` / `400`.
- **`POST /api/ingest/run`** body `{source}` (auth; `trigger_run` capability) →
  run summary `{run_id, status, counts, points}` / `403 permission_denied` /
  `400` (source not enabled / unknown source) / `409`|`500` (missing
  prerequisite). Writes an `audit_runs` row with `trigger="web"`.
- **`GET /api/audit/recent?limit=`** (auth) → `{rows: [{run_id, started_at,
  completed_at, status, trigger, scheduled_by, per_source_counts}]}`
  (non-admin: only the caller's rows; admin: all).
- **`POST /api/kb/chat`** body `{query}` (auth) → `501
  {code: "not_implemented", remediation}` (no LLM endpoint) / `400` (blank
  query). Surface only in 006 (C-1/R8).

## Dedup / ownership invariants (carried, not new)

- **One record not N (NFR-1/NFR-14)**: point IDs are content-deterministic
  (`prefix|item_key|chunk|hash`, `digital_twins.ingest.ids.point_id`); a
  web-triggered run upserts the same points as `run --once`. The owner tag is
  a *payload* field only (`owner`, `owner_tag`) and does **not** enter the
  point ID, so two owners ingesting identical content still produce one point.
- **Per-user scoping (003 R6, NFR-17)**: reads (`/api/kb/points`,
  `/api/kb/search`) filter on the caller's `owner_tag`; the audit read
  (`/api/audit/recent`) filters on `scheduled_by` for non-admins.

## State transitions

| Action | Table effect |
|---|---|
| Sign-up (fresh install, 0 accounts) | insert `accounts` row with `role=admin` (003 R7/C-4). |
| Sign-up (≥1 account exists) | insert `accounts` row with `role=reader`; duplicate email → 409 (no row). |
| Sign-in | insert `sessions` row (pbkdf2 hash, 8h TTL); set `accounts.last_active`. |
| Sign-out | flip `sessions.revoked=1` for the matching session token. |
| Web-triggered run | `audit_runs` row added (`trigger="web"`); `highwater` advanced per ingested item; Qdrant points upserted (dedup). |
| Audit read | read-only; no state change. |
