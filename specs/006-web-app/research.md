# Research & Decisions: 006 Web App

**Feature**: `specs/006-web-app` · **Slice**: BR-11.1.8 first-class web surface

This file resolves the open questions the spec leaves (marked in spec as
Rulings C-1..C-8, R7, R8) and records the reuse-vs-reimplement decisions that
keep the slice thin. No new dependencies, no new table, no new migration: the
slice reuses 001–005 and adds one HTTP app module, three config knobs, one
static UI page, and the portability/knob guard extensions.

## R1 — Skip clarify (locked)

Decision: skip the clarify step.
Rationale: BR-11.1.8, Q1, Q8, Q10 are locked in requirement.md §5; the 004
contracts already pin the MCP tool surface that the web UI mirrors. No open
questions remain.
Alternatives considered: a clarify pass to re-examine Q8 (open sign-up) — rejected
because Q8 is explicitly locked ("no OAuth / email-verification / admin-gating in
v1").

## R2 — Reuse `digital_twins.auth` + `digital_twins.state`; no parallel auth

Decision: the web app's sign-up/sign-in/sign-out all delegate to
`digital_twins.auth` (`create_account` via `accounts.create_account` for
signup; `authenticate` + `create_session` for signin; `verify_session` +
`revoke_session` for signout) and `digital_twins.state` (the `accounts` and
`sessions` tables). `web/server.py` (003) already implements
`POST /signup`, `POST /signin` on a `ThreadingHTTPServer` with these exact
helpers; `app.py` re-exposes them under `/api/auth/*` and adds signout.
Rationale: one source of truth for credentials (constitution: no duplicated
auth logic); the first-account-is-admin rule (003 R7/C-4, shared with CLI
`init`/`signup`) comes for free; the session token is the 003 pbkdf2-hashed,
8h-TTL, revocable session — reused, not reinvented.
Alternatives considered: (b) a new auth module or a new token type — rejected
(duplicate logic, two credential stores, harder to review).

## R3 — Web trigger goes through `digital_twins.ingest.pipeline`

Decision: `POST /api/ingest/run` calls
`digital_twins.ingest.pipeline.run_pipeline(db, cfg, source_names=[...],
qdrant=..., neo4j=..., embedder=..., trigger="web",
scheduled_by=<caller-email>, owner=<caller-owner-tag>)`.
Rationale: the same code path as `run --once` (CLI) and the MCP `kb_ingest`
stub (004) — no separate ingestion logic. `run_pipeline` already writes the
audit row via `start_audit_run`/`finish_audit_run` with whatever `trigger`
it is handed; passing `trigger="web"` mirrors 004's `trigger="mcp"`. The
dedup invariant (NFR-1/NFR-14) holds because point IDs are
content-deterministic and high-water marks skip unchanged items — exactly the
`run --once` behavior. The `owner=` kwarg (003 R6) stamps
`owner`/`owner_tag` on the payload for per-user scoping **without** entering
the point ID, so two owners ingesting the same content still yield one point.
Alternatives considered: a dedicated web ingest path — rejected (would
diverge from `run --once`, breaking dedup parity and constituting a
second ingestion implementation).

## R4 — Static HTML UI, no build step, no SPA framework

Decision: a single static page (e.g. `digital_twins/web/static/index.html` +
a tiny `style.css`) served by the app's static-file handler; the page uses
plain `fetch()` to `/api/*` and vanilla DOM to render. No bundler, no
framework, no template engine.
Rationale: BR-11.1.8 "no CLI required" + R4 locked ruling; keeps the shipped
surface host-neutral (no node_modules, no build artifacts) and the diff small.
The UI and an external client hit the **same** `/api/*` surface (SC-006).
Alternatives considered: a React/Vue SPA — rejected (build step, node toolchain,
host-path risk in node_modules, overkill for a minimal surface).

## R5 — `web.bind` default `127.0.0.1`; R7 — `web.port` default 8767

Decision: three new knobs in the `web` section:
- `web.bind` (str, default `127.0.0.1`, env `KB_WEB__BIND`) — loopback-only
  by default (BR-8.8); user widens to `0.0.0.0` to reach across the network.
- `web.port` (int, default **8767**, env `KB_WEB__PORT`) — a free port,
  distinct from `scheduler.status_port` (8765) and `mcp.port` (8770).
- `web.base_url` (str, default `http://localhost:8767`, env
  `KB_WEB__BASE_URL`) — optional; used to build redirect/absolute URLs.
Rationale: matches the brief (R5 "pick the port and document it"); 8767 is
the lowest free port in the 876x block that is not already used. All three
are added to `digital_twins/config/knobs.py`, `config.example.yml`,
`.env.example`, and `docs/configuration.md` and kept in lock-step by
`tests/unit/test_knob_docs.py` (constitution IV).
Alternatives considered: reuse `scheduler.status_port` — rejected (the web app
and the status server are distinct and should not collide); 8765/8770 —
already taken.

## C-1 — Chat ships the surface only (R8)

Decision: `POST /api/kb/chat` is a real endpoint (auth-checked, owner-scoped,
audit-worthy) that returns `501 {"code": "not_implemented",
"remediation": "set llm.endpoint/llm.model to enable chat"}` when no LLM
endpoint is configured. Full RAG answer generation (embed query → retrieve →
prompt LLM → return answer) is a follow-up slice.
Rationale: keeps 006 focused on the BR-11.1.8 acceptance line (reachable +
sign-in + query/trigger + dedup parity). The chat contract (auth, scoping,
audit, 501 shape) is stable, so a later slice fills generation without
re-plumbing. Mirrors 004's BR-10 stub pattern (`not_implemented_yet`).
Alternatives considered: implement full RAG now — rejected (large, LLM-dependent,
would bloat the slice and delay the dedup-parity guarantee that is the
highest-stakes property).

## C-2 — Module layout: new `digital_twins/web/app.py` that wraps `server.py`

Decision (R6 = a): add `digital_twins/web/app.py` exposing:
- `WebApp` (a `ThreadingHTTPServer`-based app) that routes `/` → static
  `index.html`, `/static/*` → static assets, `/api/*` → the REST handlers, and
  re-exposes the 003 credential endpoints under `/api/auth/*` (delegating to
  `server.py`'s `WebServer` handler logic, not duplicating it).
- A `build_web_app(db, cfg)` factory + a `serve(cfg)` entry used by the CLI.
`server.py` is **not** rewritten; `app.py` delegates to it for credentials.
Rationale: one credential source of truth (R2); small, reviewable diff; the
existing 003 tests of `server.py` stay valid.
Alternatives considered: rewrite `server.py` to add the new routes — rejected
(larger diff, risks the 003 credential tests, mixes concerns).

## C-3 — Auth surface: session token in header (or form) for `/api/*`

Decision: `/api/*` endpoints (except `/api/auth/*`) authenticate by a session
token supplied as the `Authorization: Bearer <token>` header (a fallback
`?token=` query form is accepted for the static UI's simplicity). The token is
verified with `digital_twins.auth.verify_session` (the same helper 003's
`auth_checker` uses); absent/expired/revoked → `401`. `trigger_run`-gated
endpoints additionally check the caller's role via `accounts.require_capability`
(003 R3).
Rationale: reuses the 003 session-token verification (fail-closed); the
capability gate reuses 003's `ROLE_CAPS` matrix so a reader gets `403
permission_denied` naming `trigger_run`.
Alternatives considered: cookies — rejected (more state to manage; the
session-token-in-header model already exists and is testable via
`http.client`).

## C-4 — Point count / sample / search read directly from Qdrant

Decision: `GET /api/kb/points` and `POST /api/kb/search` open a
`qdrant_client.QdrantClient` from `qdrant.url`/`qdrant.api_key` (config layer)
and read the `personal_kb` collection (`digital_twins.health.QDRANT_COLLECTION`).
Point count = collection count; owner count = count with the caller's
`owner_tag` filter; sample rows = a `scroll`/`search` of up to `limit` points
projecting `source`/`source_url`/`chunk_index`/`text`. Search = a vector
search embedding the query (reusing `digital_twins.ingest.embedding`) with an
`owner_tag` filter, returning top-N with `score`/`source_url`/`text`/
`source`/`chunk_index`.
Rationale: no separate search engine; the same vector store `run` writes to;
owner-scoping via the 003 R6 `owner_tag` payload.
Alternatives considered: a cached index — rejected (extra state to keep in
sync; Qdrant is the source of truth).

## C-5 — Reuse existing 003 web tests as the integration harness

Decision: integration tests for the new `/api/*` endpoints follow the
`tests/integration/test_web_server.py` pattern (003): build a migrated v3 DB
in `tmp_path`, construct the `WebApp` on `127.0.0.1:0`, poll for readiness,
and drive requests with `http.client`/`urllib`. The Qdrant-dependent endpoints
are exercised against an in-process `qdrant_client` (the same way 001/002
pipeline tests do) or a monkeypatched client, so no live Qdrant is required.
Rationale: reuses the proven 003 harness; keeps tests deterministic and
host-neutral.
Alternatives considered: a live-Qdrant integration test — rejected (needs a
running service; the pipeline already has in-process Qdrant test patterns).

## C-6 — CLI surface: `digital-twins web` subcommand

Decision: add a `digital-twins web` CLI subcommand that loads config, completes
migrations (reusing `cli.pre_command`), builds the web app on
`web.bind`/`web.port`, and prints the listening URL (e.g.
`web: listening on http://<bind>:<port> (UI: http://<bind>:<port>/)`). It is a
**separate** surface from `serve` (the scheduler) — it does not start the
scheduler loop; it only serves the UI + API. (An alternative of folding it into
`serve` is noted but not chosen: keeping them separate avoids coupling the
web app's lifecycle to the scheduler's pidfile/tick loop.)
Rationale: FR-013 requires a CLI surface that honors `web.bind`/`web.port`
and prints the URL; a dedicated subcommand is the cleanest way to do that
without dragging the scheduler's lifecycle into the web app.
Alternatives considered: extend `serve` to also serve the web app — rejected
(coupling; the status server binds to `scheduler.status_port` on a
separate address and has different shutdown semantics).

## C-7 — Portability guard extension (NFR-13)

Decision: add `digital_twins/web/` to the portability guard's `SHIPPED` scan —
the Python (`app.py`, `server.py`, `__init__.py`) **and** the non-Python static
assets (`static/index.html`, `static/style.css`). The guard already scans
`digital_twins` for `*.py`; a `SHIPPED_NON_PY` entry for the web static assets
(and the `web` subpackage generally) is added so host paths / usernames in the
HTML/CSS would be caught.
Rationale: FR-016 explicitly requires the guard to cover the new web
artifacts (non-Python assets too).
Alternatives considered: no guard extension — rejected (would leave the new
HTML/CSS unguarded).

## C-8 — Knob-docs guard (constitution IV)

Decision: the three new knobs are added to `digital_twins/config/knobs.py`
(the registry), `config.example.yml`, `.env.example`, and
`docs/configuration.md`. `tests/unit/test_knob_docs.py` already enforces
lock-step (registry ↔ example files ↔ docs), so the new knobs pass only when
all four surfaces are updated consistently. No new test logic is needed — the
standing guard covers it.
Rationale: SC-003; constitution IV (no undocumented knob).
Alternatives considered: a dedicated new test for web knobs — rejected (the
standing guard is the single source of truth; a second test would diverge).

## Open items carried to a later slice (explicit, not 006)

- Full LLM/RAG answer generation for `POST /api/kb/chat` (C-1/R8).
- Filling the 004 MCP `kb_search`/`kb_chat`/`kb_ingest` stubs so MCP and web
  share one implementation (006's web path uses the pipeline directly; MCP
  parity is a separate slice).
- The 0.5.0 → 0.6.0 version bump (merge-time task, not in SDD; R15 from 005).
- A "nice dashboard" beyond the minimal static UI (out of scope, A2 carried).
