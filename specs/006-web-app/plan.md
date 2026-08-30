# Implementation Plan: Web App — First-Class Web UI (Query, Chat & Ingestion)

**Input**: spec at `specs/006-web-app/spec.md`; 001–005 artifacts as the
compatibility baseline. Build on 001's `state` (SQLite v3) + `auth`,
002's `ingest/pipeline` (`run_pipeline`), 003's `web/server.py`
(`/signup`+`/signin`+session tokens + `auth_checker`), 003's `accounts`
(`ROLE_CAPS` / `require_capability`), and 001's `config` layer.

## Summary

006 ships a **first-class web surface** (BR-11.1.8): a static HTML UI + a
token-authenticated REST API under `/api/*` on a configurable
bind/port, so a user on a *different machine* can sign in with
email+password (BR-11.4.1, Q8 open sign-up), see KB point counts, search,
trigger ingestion, and inspect their own audit rows — **no CLI on that
machine**. The slice is deliberately thin: it **reuses** 001–005
(`auth`, `state`, `ingest/pipeline`, `accounts`, `web/server.py`, `config`)
and adds only a new HTTP-app module (`web/app.py`) that wraps
`web/server.py`, three new config knobs (`web.bind`/`web.port`/`web.base_url`),
one static UI page, a `digital-twins web` CLI subcommand, and the
portability/knob-docs guard extensions. No new table, no new migration,
no new dependency.

The web trigger goes through `digital_twins.ingest.pipeline.run_pipeline`
(R3) — the **same** code path as `run --once` and MCP `kb_ingest` — with
`trigger="web"` and `scheduled_by=<caller>`, so the one-record-not-N
invariant (NFR-1/NFR-14) holds: the owner tag is a payload field only,
never part of the point ID.

Out of scope (later slices): full LLM/RAG answer generation for
`POST /api/kb/chat` (006 ships the **surface only** — `501
not_implemented` until an LLM endpoint is configured, R8/C-1); filling the
004 MCP `kb_search`/`kb_chat`/`kb_ingest` stubs (web path uses the pipeline
directly); a "nice dashboard" beyond the minimal static UI (A2 carried);
the 0.5.0→0.6.0 version bump (merge-time, not in SDD, R15 from 005).

## Technical Context

| Layer | Choice |
|---|---|
| Language | Python ≥ 3.11 (001 `requires-python`; no new pin) |
| HTTP | stdlib `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` (003 `web/server.py` model; no new web framework) |
| Auth | **reuse** 001 `auth.authenticate` / `auth.create_session` / `auth.verify_session` / `auth.revoke_session` + 003 `web/server.py` `/signup`+`/signin` handlers (R2). No parallel auth system. |
| Role gate | **reuse** 003 `accounts.require_capability` + `ROLE_CAPS`; the `trigger_run` capability gates `/api/ingest/run` (reader → `403 permission_denied`). |
| State | 001 SQLite v3 (003 DDL) — `accounts`, `sessions`, `audit_runs`, `highwater` all reused **unchanged**; no new migration. |
| Ingestion | **reuse** `digital_twins.ingest.pipeline.run_pipeline(db, cfg, source_names=[...], qdrant=..., neo4j=..., embedder=..., trigger="web", scheduled_by=<caller>, owner=<owner_tag>)` (R3). |
| Vector store | 001 Qdrant `personal_kb` collection (`health.QDRANT_COLLECTION`); point count / sample / search read via `qdrant_client` (no separate search engine). |
| Config | 3 new knobs in a new `web` section: `web.bind` (`127.0.0.1`, `KB_WEB__BIND`), `web.port` (`8767`, `KB_WEB__PORT`), `web.base_url` (`http://localhost:8767`, `KB_WEB__BASE_URL`) — added to `config/knobs.py`, `config.example.yml`, `.env.example`, `docs/configuration.md` in lock-step (constitution IV, SC-003). |
| CLI | `click` (001 dep); new `digital-twins web` subcommand that binds to `web.bind`/`web.port` and prints the listening URL (FR-013, C-6). |
| UI | static single page: `web/static/index.html` + `web/static/style.css`; plain `fetch()` to `/api/*` + vanilla DOM; **no build step, no SPA framework** (R4). |
| Port | `web.port` default **8767** (R7): free; distinct from `scheduler.status_port` (8765) and `mcp.port` (8770). |
| New dependencies | **none** (stdlib + the deps 001–005 already ship: click, qdrant-client, pyyaml, etc.) |

## Architecture decisions (C-1..C-8)

- **C-1 — Chat ships the surface only (R8).** `POST /api/kb/chat` is a real
  endpoint (auth-checked, owner-scoped, audit-worthy) that returns `501
  {"code":"not_implemented","remediation":"set llm.endpoint/llm.model to
  enable chat (006 ships the surface only; R8)"}` when no LLM endpoint is
  configured. Full RAG generation (embed query → retrieve → prompt LLM) is a
  follow-up slice. Rationale: keeps 006 focused on the BR-11.1.8 acceptance
  line + the NFR-1/NFR-14 dedup invariant; the endpoint contract is stable so
  a later slice fills generation without re-plumbing auth/audit/scoping.
  Mirrors 004's BR-10 stub pattern.
- **C-2 — Module layout: new `web/app.py` wraps `server.py` (R6 = (a)).** A new
  `digital_twins/web/app.py` exposes a `WebApp` (a
  `ThreadingHTTPServer`-based app) that routes `/` → static `index.html`,
  `/static/*` → static assets, `/api/*` → the REST handlers, and re-exposes
  the 003 credential endpoints under `/api/auth/*` **by delegating to
  `server.py`'s handler logic** (not duplicating it). `server.py` is **not**
  rewritten. Rationale: one credential source of truth (R2); small,
  reviewable diff; the existing 003 tests of `server.py` stay valid.
- **C-3 — `/api/*` auth: session token in header.** Every `/api/*` endpoint
  except the three `/api/auth/*` endpoints authenticates via a session token
  supplied as `Authorization: Bearer <token>` (a `?token=` query fallback is
  accepted for the static UI). The token is verified with
  `auth.verify_session` (the same helper 003's `auth_checker` uses);
  absent/expired/revoked → `401`. `trigger_run`-gated endpoints additionally
  check the caller's role via `accounts.require_capability` (003 R3).
- **C-4 — Point count / sample / search read directly from Qdrant.**
  `GET /api/kb/points` and `POST /api/kb/search` open a `QdrantClient` from
  `qdrant.url`/`qdrant.api_key` (config layer) and read the
  `personal_kb` collection. Point count = collection count; owner count =
  count with the caller's `owner_tag` filter; sample rows = a `scroll`/
  `search` of up to `limit` points projecting `source`/`source_url`/
  `chunk_index`/`text`. Search = a vector search embedding the query
  (reusing `digital_twins.ingest.embedding`) with an `owner_tag` filter,
  returning top-N with `score`/`source_url`/`text`/`source`/`chunk_index`.
- **C-5 — Reuse the 003 web integration-test harness.** Integration tests for
  the new `/api/*` endpoints follow the `tests/integration/test_web_server.py`
  pattern (003): build a migrated v3 DB in `tmp_path`, construct the
  `WebApp` on `127.0.0.1:0`, poll for readiness, and drive requests with
  `http.client`/`urllib`. The Qdrant-dependent endpoints are exercised
  against an in-process `qdrant_client` (or a monkeypatched client) so no
  live Qdrant is required.
- **C-6 — CLI surface: `digital-twins web` subcommand.** A `click` subcommand
  that loads config, completes migrations (reusing `cli.pre_command`), builds
  the web app on `web.bind`/`web.port`, and prints the listening URL
  (`web: listening on http://<bind>:<port> (UI: http://<bind>:<port>/)`). It
  is a **separate** surface from `serve` (the scheduler) — it does not start
  the scheduler loop; it only serves the UI + API. Rationale: FR-013 requires
  a CLI surface that honors `web.bind`/`web.port` and prints the URL; a
  dedicated subcommand is the cleanest way without coupling the web app's
  lifecycle to the scheduler's pidfile/tick loop.
- **C-7 — Portability guard extension (NFR-13).** Add `digital_twins/web/`
  (the Python `app.py`/`server.py`/`__init__.py` **and** the non-Python static
  assets `static/index.html`, `static/style.css`) to the portability guard's
  `SHIPPED` scan (a `SHIPPED_NON_PY` entry for the web static assets) so host
  paths / usernames in the HTML/CSS would be caught (FR-016).
- **C-8 — Knob-docs guard (constitution IV).** The three new knobs are added
  to `digital_twins/config/knobs.py` (registry), `config.example.yml`,
  `.env.example`, and `docs/configuration.md`. `tests/unit/test_knob_docs.py`
  already enforces lock-step (registry ↔ example files ↔ docs), so the new
  knobs pass only when all four surfaces are updated consistently. No new
  test logic is needed — the standing guard covers it.

## File-level changes

### New files

```
digital_twins/
├── web/
│   ├── app.py             # NEW — the /api/* REST app + static UI + auth wiring
│   │                      #   (WebApp, build_web_app, serve). Wraps server.py
│   │                      #   (R6=C-2); reuses auth/state/accounts/pipeline
│   │                      #   (R2/R3). Endpoints: /api/me, /api/auth/*,
│   │                      #   /api/kb/points, /api/kb/search, /api/ingest/run,
│   │                      #   /api/audit/recent, /api/kb/chat.
│   └── static/            # NEW — the static UI (no build step, R4)
│       ├── index.html     #   sign-in, sign-up, KB panel (count/search/trigger/audit)
│       └── style.css      #   minimal styling
tests/
├── unit/
│   ├── test_web_app_auth.py    # NEW — /api/auth/* (signup/signin/signout) semantics +
│   │                           #   /api/me; reuses the 003 harness (C-5)
│   ├── test_web_app_kb.py      # NEW — /api/kb/points + /api/kb/search (in-process
│   │                           #   Qdrant or monkeypatched client; 400 blank query;
│   │                           #   401 no token; 502/503 qdrant down)
│   ├── test_web_app_ingest.py  # NEW — /api/ingest/run: trigger="web" audit row,
│   │                           #   owner_tag stamp, dedup parity vs run --once,
│   │                           #   403 reader (trigger_run), 400 source-not-enabled/
│   │                           #   unknown-source, 409/500 missing-prereq
│   ├── test_web_app_audit.py   # NEW — /api/audit/recent: non-admin → own rows only
│   │                           #   (scheduled_by), admin → all; NFR-16
│   ├── test_web_app_chat.py    # NEW — /api/kb/chat: 501 not_implemented (no LLM
│   │                           #   endpoint) + 400 blank query + auth/owner scoping
│   ├── test_web_cli.py         # NEW — `digital-twins web` subcommand: binds to
│   │                           #   web.bind/web.port, prints the listening URL,
│   │                           #   honors the knobs (FR-013 / C-6)
│   └── test_web_config_knobs.py# NEW — web.bind/web.port/web.base_url default +
│                               #   env-var mapping (KB_WEB__*) ; lock-step is also
│                               #   covered by test_knob_docs.py (C-8)
└── integration/
    └── test_web_app.py         # NEW — end-to-end: sign up → sign in → /api/me →
                                #   kb/points → kb/search → ingest/run → audit/recent
                                #   over the live WebApp on 127.0.0.1:0 (the 003
                                #   harness, C-5); dedup parity web-vs-run --once
```

### Modified files

```
digital_twins/
├── config/knobs.py        # + GROUP_WEB + web.bind / web.port / web.base_url
│                           #   (R5/R7/C-8); env KB_WEB__BIND/PORT/BASE_URL
├── cli.py                 # + `web` subcommand (C-6): build the web app on
│                           #   web.bind/web.port, print the listening URL, serve
├── __main__.py            # (unchanged; cli.web is reachable via digital-twins web)
docs/
└── configuration.md       # + web section (web.bind/web.port/web.base_url) —
                           #   lock-step with config.example.yml + .env.example +
                           #   config/knobs.py (C-8)
tests/
├── integration/test_portability.py # + digital_twins/web/ in SHIPPED; +
│                                  #   SHIPPED_NON_PY entries for the static assets
│                                  #   (C-7/NFR-13)
config.example.yml         # + web: bind/port/base_url (with the 8767 default)
.env.example               # + KB_WEB__BIND / KB_WEB__PORT / KB_WEB__BASE_URL
```

> **Backward-compat invariant**: no 001–005 call site breaks. `web/server.py`
> is **not** modified (R6 = (a)); `run_pipeline` is called with the existing
> `trigger`/`scheduled_by`/`owner` kwargs (003 already supports all three);
> `auth`/`state`/`accounts` are reused, not changed. The new knobs are
> additive (a new `web` section). The standing guards —
> `tests/integration/test_portability.py` (gains the web assets) and
> `tests/unit/test_knob_docs.py` (the new knobs must be lock-step) — re-run at
> completion (SC-003).

## Dependencies (what lands before what)

```
Phase 1 (Foundational — config knobs + web app scaffold)
  1.  web.bind / web.port / web.base_url knobs (config/knobs.py, R5/R7)
      <- nothing; lock-step with config.example.yml/.env.example/docs (C-8)
  2.  WebApp scaffold + build_web_app + static / + /static/* handlers (C-2/R4)
      <- (1) [reads web.bind/web.port]

Phase 2 (Auth surface — reuse 003)
  3.  /api/auth/signup + /api/auth/signin + /api/auth/signout (R2/C-3)
      <- (2) [delegates to server.py; reuses auth + state]
  4.  /api/me (auth-checked; email/role/point_count)
      <- (3)

Phase 3 (KB read surface — Qdrant)
  5.  /api/kb/points (count + owner_count + sample) (C-4)
      <- (2),(4) [auth via (3); Qdrant read]
  6.  /api/kb/search (owner-scoped vector search) (C-4)
      <- (5) [reuses the Qdrant read + embedding]

Phase 4 (Ingestion + audit — reuse pipeline)
  7.  /api/ingest/run (trigger="web"; trigger_run gate; owner_tag; dedup)
      <- (3) [reuses run_pipeline (R3); role gate via accounts]
  8.  /api/audit/recent (per-user; NFR-16)
      <- (7) [reads audit_runs]
  9.  /api/kb/chat (surface only; 501 until LLM endpoint) (C-1/R8)
      <- (3) [auth + owner scoping; reads llm.endpoint from config]

Phase 5 (CLI + UI + guards)
  10. digital-twins web CLI subcommand (C-6/FR-013)
      <- (2),(7) [builds + serves the WebApp]
  11. static UI: index.html + style.css (sign-in/sign-up/KB panel) (R4)
      <- (3),(5),(6),(7),(8) [drives /api/*]
  12. portability guard: add digital_twins/web/ + static assets (C-7)
      <- (11) [guards the new HTML/CSS]
  13. knob-docs lock-step: web.* in all four surfaces (C-8) [part of (1),
      verified here]

Polish
  14. quickstart.md (host-neutral, NFR-13)
  15. end-to-end integration test (sign up → sign in → kb → ingest → audit)
      <- (3),(4),(5),(6),(7),(8)
```

Critical path: **1 → 2 → 3 → 4 → 7 → 10** (knobs → app scaffold → auth →
me → ingest → CLI). Phases 3 and 4 are parallel after Phase 2. The static UI
(11) and the guards (12/13) land last, after the API is stable.

## Risks

| Risk | Mitigation |
|---|---|
| **Dedup parity broken by the web path** (a web-triggered run produces a *different* point than `run --once` for the same content → one-record-not-N violated, NFR-1/NFR-14) | `/api/ingest/run` calls `run_pipeline` (R3) with the **same** code path as `run --once`; the owner tag is a payload-only field (003 R6) and never enters the point ID. A red test ingests the same content via `run --once` and then via `/api/ingest/run` and asserts the collection count is unchanged (SC-002). |
| **`/api/ingest/run` bypasses the role gate** (a reader triggers a run) | The endpoint checks `accounts.require_capability(caller, "trigger_run")` **before** calling `run_pipeline`; reader → `403 permission_denied` (003 R3). A red test asserts the refusal and that no audit row is written on refusal. |
| **Session-token verification diverges from 003's `auth_checker`** (a token 003's status server accepts is rejected by the web app, or vice-versa) | Both call `auth.verify_session` (C-3) — the same helper 003's `auth_checker` uses. A red test round-trips a session token through both the web `/api/*` gate and 003's `auth_checker` and asserts both accept it. |
| **Qdrant unreachable → crash** (points/search) | The Qdrant read is wrapped; an unreachable client → `502`/`503` with a remediation hint, not a traceback. A red test (monkeypatched client that raises) asserts `502`/`503` + no crash. |
| **Chat is mistaken for "done"** (someone thinks RAG generation shipped in 006) | The endpoint returns `501 not_implemented` + a remediation hint naming `llm.endpoint`; the quickstart and the spec both say "surface only, R8/C-1." A red test asserts the `501` shape when no LLM endpoint is set. |
| **Host path / username leaks into the static HTML/CSS** (NFR-13) | The UI is written host-neutral (relative `/api/*` paths, `localhost` placeholders only in docs); the portability guard (C-7) scans the new `digital_twins/web/` **including** the static assets, so a leaked host path fails the guard. |
| **`web.port` collides with 8765/8770** | `web.port` defaults to **8767** (R7), a free port distinct from `scheduler.status_port` (8765) and `mcp.port` (8770); documented in the three config surfaces (C-8). |
| **`web` subcommand drags in the scheduler lifecycle** | `digital-twins web` is a separate subcommand (C-6) — it does not start the scheduler loop, does not write the scheduler pidfile; it only builds + serves the `WebApp`. A red test asserts the subcommand does not invoke the scheduler. |
| **001's `serve`/status server already uses `ThreadingHTTPServer`** (two servers, confusion) | The web app is a **separate** `WebApp` on `web.bind`/`web.port` (8767), distinct from `serve`'s status server on `scheduler.status_port` (8765). Both bind loopback by default; no shared port. |

## Testing strategy (Test-First, constitution III)

Every task in `tasks.md` has a **red test written before its code**. The
automated checks map to the spec's success criteria:

- **SC-001 (second-machine sign-in, no CLI)**:
  `tests/unit/test_web_app_auth.py` + `tests/integration/test_web_app.py` —
  sign up (first → admin, else reader), duplicate → 409, sign in → session
  token, bad password → 401, sign out → token revoked; the token is accepted
  by `/api/me`. Driven over the live `WebApp` on `127.0.0.1:0` (the 003
  harness, C-5).
- **SC-002 (dedup parity web vs `run --once`)**:
  `tests/unit/test_web_app_ingest.py` + `tests/integration/test_web_app.py` —
  ingest the same content via `run --once`, then via `/api/ingest/run`; assert
  the Qdrant point count is unchanged and an `audit_runs` row with
  `trigger="web"` + `scheduled_by=<caller>` is written.
- **SC-003 (three knobs in lock-step)**:
  `tests/unit/test_web_config_knobs.py` + the standing
  `tests/unit/test_knob_docs.py` — `web.bind`/`web.port`/`web.base_url` present
  in `config/knobs.py`, `config.example.yml`, `.env.example`, and
  `docs/configuration.md` identically.
- **SC-004 (401/403 fail-closed)**:
  `tests/unit/test_web_app_kb.py` / `_ingest.py` / `_chat.py` — every
  `/api/*` endpoint (except `/api/auth/*`) is `401` without a valid token;
  `/api/ingest/run` is `403 permission_denied` for a reader (naming
  `trigger_run`).
- **SC-005 (per-user audit)**: `tests/unit/test_web_app_audit.py` — a
  non-admin's `/api/audit/recent` lists only their rows
  (`scheduled_by=<caller>`); an admin's lists all.
- **SC-006 (static UI drives `/api/*` only)**:
  `tests/integration/test_web_app.py` — `GET /` returns `index.html`;
  `/static/style.css` returns 200; the page's `fetch` targets are the
  `/api/*` paths (no parallel client-side logic).
- **Standing guards (re-run)**: `tests/integration/test_portability.py`
  (gains `digital_twins/web/` + the static assets, C-7) and
  `tests/unit/test_knob_docs.py` (the new web knobs, C-8) must stay green.
