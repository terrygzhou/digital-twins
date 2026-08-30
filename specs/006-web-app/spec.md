# Feature Specification: Web App — First-Class Web UI for Query, Chat & Ingestion

**Feature Branch**: `006-web-app`

**Created**: 2026-08-30

**Status**: Complete (v0.5.0, all tasks done)

**Input**: User description: "006 — Web App First-Class Surface (BR-11.1.8): ship a web application under the `digital-twins` package so a user on a different machine (phone, laptop, remote host) can `pip install digital-twins`, point `KB_STATE_DIR` at their own Qdrant/Neo4j/LLM endpoints, widen the bind address via config (e.g. `web.bind=0.0.0.0` / `KB_WEB__BIND`), `digital-twins serve` a web UI, sign in with email + password (BR-11.4.1, Q8: open sign-up on a fresh install, no OAuth / email-verification / admin-gating), and query the KB / chat / trigger ingestion from the browser — no CLI required (BR-11.1.8)."

## User Scenarios & Testing *(mandatory)*

<!--
  User stories are PRIORITIZED user journeys ordered by importance. Each is
  INDEPENDENTLY TESTABLE: implementing just one yields a viable MVP.
-->

### User Story 1 — Sign in from another machine (Priority: P1)

A user on a phone, laptop, or remote host that has **no** CLI install signs in to their KB from a browser: they open `http://<host>:<web.port>`, create an account (open sign-up on a fresh install, Q8) or sign in with an existing email + password, and receive a session token. The web app reuses the existing `digital_twins.auth` + `digital_twins.state` account/session machinery — there is no parallel auth system, and the first-ever account becomes `admin`, matching the CLI `init`/`signup` path (003 R7/C-4).

**Why this priority**: this is the slice's reason to exist (BR-11.1.8) — a *first-class* surface reachable from a different machine with email+password sign-in (BR-11.4.1). Nothing else is usable without it.

**Independent Test**: A fresh install (all sources disabled) on `web.bind=0.0.0.0` is reachable over the network from a second host: `POST /api/auth/signup` returns `{email, role: "admin"}` for the first account and `role: "reader"` for a second; a duplicate email returns 409; `POST /api/auth/signin` with valid credentials returns a `session_token` accepted by `digital_twins.auth.verify_session`; a bad password returns 401; `POST /api/auth/signout` revokes the session. No CLI is required.

**Acceptance Scenarios**:

1. **Given** a fresh install and a browser on a different machine, **When** they `POST /api/auth/signup` with a new email+password, **Then** they get `200 {email, role: "admin", created: true}` (first account) and a `200` sign-in immediately after that returns a valid session token.
2. **Given** two accounts exist, **When** a third signup uses the first's email, **Then** the server returns `409 {"error": "account already exists"}`.
3. **Given** a known account, **When** they sign in with a wrong password, **Then** the server returns `401` and no session token.
4. **Given** a signed-in session token, **When** they `POST /api/auth/signout`, **Then** the session is revoked and the same token no longer authenticates any `/api/*` request.
5. **Given** a request to any `/api/*` endpoint (other than auth endpoints) with no or an invalid session token, **Then** the server returns `401`.

---

### User Story 2 — See KB point counts (Priority: P1)

A signed-in user opens the KB panel and sees how many points are in their collection, and a small sample of the most recent rows (source + source_url + chunk text preview). This is the read-only "what's in my KB" view — no embedding, no LLM call, just a count + sample query against Qdrant scoped to the caller.

**Why this priority**: it is the first *useful* read surface after sign-in; it proves the web app can reach the user's Qdrant and read back what they've ingested (the "reachable from another machine + works against my endpoints" half of BR-11.1.8).

**Independent Test**: With points already in Qdrant, `GET /api/kb/points?limit=5` returns the total count in the `personal_kb` collection, an owner-scoped count (points stamped with the caller's owner tag), and up to `limit` sample rows each carrying `source`, `source_url`, `chunk_index`, and a `text` preview. With an empty collection the count is 0 and the sample is an empty list.

**Acceptance Scenarios**:

1. **Given** the caller's owner_tag is stamped on N points in Qdrant, **When** they `GET /api/kb/points`, **Then** the response includes the collection-wide count and an `owner_count` of N.
2. **Given** the caller requests `?limit=3` and the collection has ≥3 points, **When** the request is served, **Then** exactly 3 sample rows are returned, each with `source`, `source_url`, `chunk_index`, `text`.
3. **Given** the Qdrant collection is empty, **When** the caller requests points, **Then** the count is 0 and the sample list is empty (not an error).

---

### User Story 3 — Search the KB from the browser (Priority: P1)

A signed-in user types a query into the KB panel and gets back the top-N matching chunks with a relevance score and the source URL — the same `kb_search`-style result an MCP client would get, produced by the same query path (no separate search logic). No CLI required.

**Why this priority**: search is the primary "read" interaction of a KB app (BR-11.1.8 "query the KB") and it is the surface users will actually try after sign-in.

**Independent Test**: With a populated collection, `POST /api/kb/search` with body `{query, limit}` returns a `results` list of top-N items, each with `score` (cosine/distance-derived), `source_url`, `text`, `source`, and `chunk_index`. The same content searched via MCP `kb_search` (once 004's stub is filled in a later slice) would yield the same point set — the web path uses the same vector search as `run`-style queries. An empty/blank query returns `400` with a clear error.

**Acceptance Scenarios**:

1. **Given** a populated collection and a meaningful query, **When** the caller `POST`s `{query: "...", limit: 5}`, **Then** the response returns up to 5 results, each with `score`, `source_url`, `text`, `source`, `chunk_index`, sorted by descending score.
2. **Given** a caller's owner_tag filter, **When** they search, **Then** results are scoped to the caller's owner (matching points owned by the caller), not the whole collection.
3. **Given** a blank or missing query, **When** the caller searches, **Then** the server returns `400` with `{"error": "query must be a non-empty string"}`.

---

### User Story 4 — Trigger ingestion from the browser (Priority: P1)

A signed-in user with the `trigger_run` capability (admin/scheduler role) clicks "Trigger ingestion" for an enabled source (or "run all enabled") and the package's ingestion pipeline runs **on the same code path as `digital-twins run --once` and MCP `kb_ingest`** — no separate ingestion logic (R3). The audit row gets `trigger="web"` (mirroring 004's MCP `trigger="mcp"`) and `scheduled_by=<caller-email>`. The same content ingested via the web UI yields the **same points** as `run --once` for the same source + window (NFR-1, NFR-14 — one record, not N).

**Why this priority**: "trigger ingestion from the browser — no CLI required" is the explicit BR-11.1.8 acceptance line; the dedup-parity invariant (NFR-1/NFR-14) is the highest-stakes correctness property of the whole slice.

**Independent Test**: With a source enabled and its prerequisites present, `POST /api/ingest/run` with `{source: "fs"}` runs `digital_twins.ingest.pipeline.run_pipeline` with `trigger="web"` and `scheduled_by=<caller>`; the audit row is written with `trigger="web"`; the returned run summary (`run_id`, `status`, per-source counts, points) matches what `run --once` produces for the same source; re-running the same source does **not** duplicate points (high-water + deterministic IDs). A reader-role caller gets `403 permission_denied`. A source that is disabled in config or missing prerequisites fails fast with a named error and a `failed` audit row (constitution IV).

**Acceptance Scenarios**:

1. **Given** an enabled source with prerequisites, **When** an admin/scheduler `POST /api/ingest/run {source}`, **Then** `run_pipeline` executes with `trigger="web"`, an audit row is written with `trigger="web"` and `scheduled_by=<caller>`, and the response carries the run summary.
2. **Given** the same source + window already ingested by `run --once`, **When** the caller triggers it again from the web UI, **Then** the total point count in the collection is unchanged (one record, not N — NFR-1/NFR-14).
3. **Given** a reader-role caller, **When** they `POST /api/ingest/run`, **Then** the server returns `403` with `code=permission_denied` naming the missing `trigger_run` capability (003 R3 role model), and no audit row is written.
4. **Given** a source that is disabled in config, **When** the caller requests to run it, **Then** the server returns `400` naming the source as not enabled (fail-fast, constitution IV).
5. **Given** an enabled source missing a prerequisite, **When** the caller runs it, **Then** the server returns `409`/`500` naming the missing prerequisite and a `failed` audit row is written (fail-fast; never silently ingest zero items).

---

### User Story 5 — Inspect run history / audit (Priority: P2)

A signed-in user opens the audit panel and sees the last N audit rows **for their own runs** (NFR-16: a user lists only their own; an admin may list all). Each row carries `run_id`, `started_at`, `completed_at`, `status`, `trigger`, `scheduled_by`, `per_source_counts`. This closes the loop after a web-triggered run: the user can confirm it ran and what it did.

**Why this priority**: auditability is a constitutional principle (V) and the "shows the last run's status/audit row (NFR-16)" UI line; it is P2 because it is read-only and depends on runs having happened.

**Independent Test**: `GET /api/audit/recent?limit=10` for a non-admin returns only rows where `scheduled_by=<caller>`; for an admin it returns all rows. The shape matches the 001/002/004 audit record. A run just triggered via `/api/ingest/run` is visible here immediately.

**Acceptance Scenarios**:

1. **Given** runs by alice and bob, **When** alice `GET /api/audit/recent`, **Then** only alice's rows appear.
2. **Given** an admin, **When** they `GET /api/audit/recent`, **Then** all users' rows appear (documented admin privilege).
3. **Given** a web-triggered run just completed, **When** the caller lists recent audit, **Then** the row is present with `trigger="web"` and `scheduled_by=<caller>`.

---

### User Story 6 — Chat with the KB from the browser (Priority: P3)

A signed-in user can ask a natural-language question and get a grounded answer from the KB. **Scope ruling (C-1):** 006 ships the *surface* — the `POST /api/kb/chat` endpoint and the UI affordance — wired to the **same** LLM + retrieval path that `kb_chat` would use, but a full RAG answer engine is **out of scope** for this slice: if no LLM endpoint is configured the endpoint returns a clean `501 not_implemented` (mirroring 004's BR-10 stub behavior) with a remediation hint. The endpoint, its auth, its owner-scoping, and its audit row are real; the LLM generation is the follow-up slice. (See Rulings / research.)

**Why this priority**: "chat" is in the BR-11.1.8 line, but a full RAG answer engine is a large, LLM-dependent capability. 006 delivers the contract + surface now so a later slice can fill the generation without re-plumbing auth/audit/scoping. It is P3 and the last to be testable end-to-end.

**Independent Test**: With no LLM endpoint configured, `POST /api/kb/chat {query}` returns `501 {"error": "not_implemented", "remediation": "set llm.endpoint..."}`. With an LLM endpoint configured the endpoint is reachable, auth-checked, owner-scoped, and audit-worthy (the actual answer generation is a later slice). A blank query returns `400`.

**Acceptance Scenarios**:

1. **Given** no `llm.endpoint` configured, **When** the caller `POST /api/kb/chat {query}`, **Then** the server returns `501` with `code=not_implemented` and a remediation hint naming `llm.endpoint` (not a crash, not a 401).
2. **Given** a blank query, **When** the caller chats, **Then** the server returns `400`.
3. **Given** a valid signed-in caller and a configured LLM, **When** they chat, **Then** the request is authenticated, owner-scoped, and (once generation lands) returns an answer grounded in the caller's points. (Generation itself is a follow-up slice.)

---

### Edge Cases

- **Bind address**: `web.bind` defaults to `127.0.0.1` (BR-8.8 loopback-only). Widening to `0.0.0.0` is a user config decision; the app must bind exactly to the configured address and print the listening URL (with the configured bind) at startup.
- **Fresh install, zero accounts**: open sign-up (Q8) — no admin-gating; the first account is `admin` (003 R7/C-4, shared with CLI `init`/`signup`).
- **Concurrent requests**: the web app must be safe under concurrent browser tab requests (ThreadingHTTPServer model, like 003's server) — the state store is shared; reads are consistent.
- **Session expiry / revocation**: an expired or revoked session token is treated as 401 on every `/api/*` call (fail-closed, mirroring 003's `auth_checker` behavior).
- **No enabled sources**: `/api/ingest/run` with no enabled source returns a clear "no sources enabled" error (fail-fast, constitution IV) and writes a `failed` audit row.
- **Unknown source name**: a source name not in `cfg["sources"]` (and not a built-in) returns `400` naming the unknown source (mirrors `run --once`'s `UnknownSourceError`).
- **Qdrant down**: point-count/search endpoints that can't reach Qdrant return `502`/`503` with a remediation hint, not a crash.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a web app that serves a static UI + a token-authenticated REST API on a configurable bind address/port, so a user on a **different machine** can sign in and use the KB with **no CLI required** (BR-11.1.8).
- **FR-002**: The web app MUST reuse `digital_twins.auth` (session tokens, pbkdf2) and `digital_twins.state` (accounts/sessions) for sign-up/sign-in/sign-out — it MUST NOT duplicate auth logic (R2). The first-ever account MUST resolve to role `admin`, else `reader` (003 R7/C-4).
- **FR-003**: The web app MUST expose `GET /api/me` returning the caller's email, role, and a points count, authenticated by session token.
- **FR-004**: The web app MUST expose `POST /api/auth/signup`, `POST /api/auth/signin`, `POST /api/auth/signout` with the 003 credential-endpoint semantics (200/409/401; signout revokes the session).
- **FR-005**: The web app MUST expose `GET /api/kb/points?source=&limit=` returning the collection point count, an owner-scoped count, and up to `limit` sample rows (each with `source`, `source_url`, `chunk_index`, `text`).
- **FR-006**: The web app MUST expose `POST /api/kb/search` with body `{query, limit}` returning top-N results, each with `score`, `source_url`, `text`, `source`, `chunk_index`, scoped to the caller's owner; blank query → 400.
- **FR-007**: The web app MUST expose `POST /api/ingest/run` with body `{source}` (or "all enabled") that runs `digital_twins.ingest.pipeline.run_pipeline` with `trigger="web"` and `scheduled_by=<caller>` — the **same** pipeline path as `run --once` and MCP `kb_ingest` (R3); the audit row gets `trigger="web"`.
- **FR-008**: The web-triggered ingestion MUST be owner-scoped (caller's `owner_tag`) and MUST satisfy the one-record-not-N invariant: the same content ingested via the web UI yields the same points as `run --once` for the same source + window (NFR-1, NFR-14).
- **FR-009**: The web app MUST gate `/api/ingest/run` on the `trigger_run` capability (003 R3 role model); a reader-role caller gets `403 permission_denied` with the missing capability named.
- **FR-010**: The web app MUST expose `GET /api/audit/recent?limit=` returning the last N audit rows for the caller (non-admin: only their own rows by `scheduled_by`; admin: all rows), in the 001/002/004 audit record shape (NFR-16).
- **FR-011**: The web app MUST expose `POST /api/kb/chat` with body `{query}` as the chat **surface**: authenticated, owner-scoped, audit-worthy, and returning `501 not_implemented` with a remediation hint when no LLM endpoint is configured (full RAG generation is a follow-up slice; C-1).
- **FR-012**: The web app MUST honor three new config knobs — `web.bind` (default `127.0.0.1`), `web.port` (a free default port, distinct from `scheduler.status_port` 8765 and `mcp.port` 8770), and `web.base_url` (default derived from `http://localhost:<web.port>`, optional, for redirect URLs) — each documented in `config.example.yml`, `.env.example`, and `docs/configuration.md` and kept in lock-step by `tests/unit/test_knob_docs.py` (constitution IV).
- **FR-013**: The system MUST provide a CLI surface to start the web app (a `digital-twins web` subcommand, or an extension of `serve`) that binds to `web.bind`/`web.port` and prints the listening URL (constitution IV: it must honor the knobs, not hard-code an address).
- **FR-014**: The web app MUST be reachable from a browser on a different machine after the user widens the bind address (e.g. `web.bind=0.0.0.0` / `KB_WEB__BIND=0.0.0.0`) and signs in with email + password (BR-11.1.8, BR-11.4.1, Q8).
- **FR-015**: The static UI MUST be plain HTML (no build step, no SPA framework) and provide: a sign-in form, a sign-up form (first-time), and a "KB" panel that shows the point count, lets the user run a search query, has a "Trigger ingestion" button (per-source or run-all-enabled), and shows the last run's status/audit row (NFR-16). The UI MUST hit the **same** `/api/*` surface an external client hits.
- **FR-016**: The web app MUST be host-neutral: no host path, username, or install location in shipped code or docs (NFR-13); `tests/integration/test_portability.py` MUST stay green and MUST gain `digital_twins/web/` (including non-Python UI assets) in its `SHIPPED` scan.
- **FR-017**: Every `/api/*` endpoint (except the public auth endpoints) MUST reject an absent, expired, or revoked session token with `401` (fail-closed), reusing the 003 session-token verification.

### Key Entities

- **Account**: an email + role (`admin`/`scheduler`/`reader`) + password hash; created by open sign-up on a fresh install (first = admin, else reader). Reuses 001/003 `accounts` table.
- **Session**: a short-lived, revocable server-side session token (pbkdf2-hashed, 8h TTL) bound to an account email. Reuses 003 `sessions` table.
- **KB Point**: a chunk in the `personal_kb` Qdrant collection, with payload `source`, `source_url`, `item_key`, `chunk_index`, `ts`, `text`, and (when owner-stamped) `owner`/`owner_tag`. Read by the KB panel/search.
- **Audit Run**: a row in `audit_runs` (`run_id`, `started_at`, `completed_at`, `status`, `trigger`, `scheduled_by`, `per_source_counts`); a web trigger writes `trigger="web"`.
- **Config Knobs**: `web.bind`, `web.port`, `web.base_url` — new `web`-section knobs with env-var mapping (`KB_WEB__BIND`, `KB_WEB__PORT`, `KB_WEB__BASE_URL`).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a fresh install with `web.bind=0.0.0.0`, a browser on a *different* machine can sign up (first account → admin), sign in, and reach `/api/me` — all with **no CLI on that machine** (BR-11.1.8).
- **SC-002**: The same content ingested via the web UI's "Trigger ingestion" yields the **same** points as `digital-twins run --once` for the same source + window (one record, not N — NFR-1, NFR-14); a `trigger="web"` audit row is written.
- **SC-003**: All three new knobs (`web.bind`, `web.port`, `web.base_url`) are present in `digital_twins/config/knobs.py`, `config.example.yml`, `.env.example`, and `docs/configuration.md`, and `tests/unit/test_knob_docs.py` + `tests/integration/test_portability.py` stay green (constitution IV, NFR-13).
- **SC-004**: Every `/api/*` endpoint except auth is 401 without a valid session token; `trigger_run`-gated endpoints are 403 for reader role (003 R3, NFR-17).
- **SC-005**: A non-admin's `/api/audit/recent` lists only their own rows; an admin's lists all (NFR-16).
- **SC-006**: The static UI (plain HTML, no build step) renders sign-in, sign-up, and the KB panel (point count, search, trigger-ingest, last-audit-row) and drives them through `/api/*` only (no parallel client-side logic).

## Assumptions

- 001–005 are complete at v0.5.0; `digital_twins.auth`, `digital_twins.state`, `digital_twins.ingest.pipeline`, `digital_twins.mcp`, `digital_twins.web.server` (003 /signup + /signin + session tokens) exist and are reused, not re-implemented.
- The user has their own Qdrant/Neo4j/LLM endpoints configured (`qdrant.url`, etc.); the web app reads them through the config layer at runtime (no host defaults).
- The UI is minimal/static (A2-style, carried forward): it is a browser surface over the `/api/*` REST surface, not a rich dashboard; a "nice dashboard" is out of scope.
- The web app and the scheduler's status server (`scheduler.status_port`, 8765) and the MCP server (`mcp.port`, 8770) are distinct; `web.port` is a new, distinct default (pinned in the brief below as 8767).
- Version stays `0.5.0` during the SDD loop; the bump to `0.6.0` is a merge-time task, not part of SDD (R15, carried from 005).
- All owner decisions Q1–Q10 are locked (requirement.md §5); Q8 (open sign-up, no OAuth/email-verification/admin-gating) and Q10 (no cron parser) govern this slice and are not re-litigated.
- LLM endpoint configuration (`llm.endpoint`/`llm.model`/`llm.api_key`) is already a 001 knob; the chat surface reuses it and degrades to `501` when unset (C-1).

## Rulings (controller pre-flight, honored — not re-opened)

- **R1**: Skip clarify — BR-11.1.8, Q1, Q8, Q10 are locked; the 004 contracts pin the tool surface.
- **R2**: The web app reuses `digital_twins.auth` + `digital_twins.state` — no parallel auth.
- **R3**: The web trigger goes through `digital_twins.ingest.pipeline` (the same code path as `run --once` and MCP `kb_ingest`) — no separate ingestion logic. The audit row gets `trigger="web"`.
- **R4**: Static HTML UI, no build step, no SPA framework.
- **R5**: `web.bind` defaults to `127.0.0.1` (BR-8.8); the port is picked and documented in this slice.
- **R6**: The web app is a NEW module that wraps/extends the existing `digital_twins/web/server.py`. **(a) vs (b) ruling, made at SDD pre-flight before Task 1 dispatch:** **Ruling = (a)** — add a new `digital_twins/web/app.py` (HTTP app: static UI + `/api/*` REST + auth wiring) that *reuses* `web/server.py`'s existing credential handlers (`WebServer` / session-token helpers) and `digital_twins.auth`/`state`; it does NOT rewrite `server.py` and does NOT introduce a second auth system. Rationale: `server.py` already holds the proven `/signup`+`/signin`+`/status`+session-token plumbing; re-wrapping it keeps one source of truth for credentials, satisfies R2, and keeps the diff small and reviewable. Cost if wrong: a thin indirection layer between `app.py` and `server.py`'s handlers; mitigated by having `app.py` delegate to `server.py` rather than duplicate.
- **R7 (port)**: `web.port` default = **8767** (free; distinct from `scheduler.status_port` 8765 and `mcp.port` 8770; not `8765`). Documented in `config.example.yml`, `.env.example` (`KB_WEB__PORT`), and `docs/configuration.md`.
- **R8 (chat scope)**: `POST /api/kb/chat` ships the **surface only** in 006 (auth, owner-scoping, audit-worthy, `501 not_implemented` + remediation when no LLM endpoint); full RAG answer generation is a follow-up slice. Rationale: keeps 006 focused on the BR-11.1.8 "reachable + sign-in + query/trigger" acceptance line and the NFR-1/NFR-14 dedup invariant; a full LLM answer engine is a large, LLM-dependent capability that would bloat this slice. Cost if wrong: chat is not end-to-end usable in 0.5.0; but the endpoint contract is stable so a later slice fills generation without re-plumbing auth/audit/scoping.
