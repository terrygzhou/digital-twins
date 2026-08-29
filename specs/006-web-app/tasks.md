# Tasks: Web App — First-Class Web UI (006)

**Input**: Design documents from `/specs/006-web-app/` (spec.md, plan.md, research.md, data-model.md, contracts/web-api.md, quickstart.md)

**Prerequisites**: spec.md (required), plan.md (required), research.md, data-model.md, contracts/

**Tests**: Included — constitution III (Test-First, NON-NEGOTIABLE) requires each story's tests to be written FIRST and to FAIL before implementation.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US6)

## Path Conventions

Single project: `digital_twins/` and `tests/` at repository root (per plan.md structure).

---

## Phase 1: Setup (Web Config Knobs)

**Purpose**: Add the three new `web.*` config knobs to the registry and all four config surfaces in lock-step (constitution IV), so the rest of the feature can read them through the config layer.

- [x] T001 [P] [US1] RED: web.bind/web.port/web.base_url knob registry + env-mapping tests — create `tests/unit/test_web_config_knobs.py` asserting the three new knobs in `digital_twins/config/knobs.py`: `web.bind` (default `127.0.0.1`, env `KB_WEB__BIND`), `web.port` (default `8767`, env `KB_WEB__PORT`, distinct from `scheduler.status_port` 8765 and `mcp.port` 8770), `web.base_url` (default `http://localhost:8767`, env `KB_WEB__BASE_URL`). Also assert the lock-step surface: each knob present in `config.example.yml` (a `web:` section with bind/port/base_url), `.env.example` (`KB_WEB__BIND`/`KB_WEB__PORT`/`KB_WEB__BASE_URL`), and `docs/configuration.md` (a web section row per knob with matching type/default/env). Run `pytest tests/unit/test_web_config_knobs.py` and confirm RED — the web section does not exist yet.
- [x] T002 [US1] GREEN: add GROUP_WEB knobs + update all four config surfaces — add `GROUP_WEB` with `web.bind` (`127.0.0.1`), `web.port` (`8767`), `web.base_url` (`http://localhost:8767`) to `digital_twins/config/knobs.py`; add the matching `web:` section to `config.example.yml`, `KB_WEB__BIND`/`KB_WEB__PORT`/`KB_WEB__BASE_URL` to `.env.example`, and a web section table to `docs/configuration.md` — all in lock-step (constitution IV) so `test_knob_docs.py`'s existing lock-step classes stay green and the T001 red test passes. R5/R7: bind defaults to loopback (BR-8.8); port 8767 documented as free. T001 blocks T002.

---

## Phase 2: WebApp Scaffold (US1)

**Purpose**: The `WebApp` class — static routes, bearer-token gate, `/api/*` dispatch — reusing `digital_twins.auth` + `digital_twins.state` (no parallel auth, R2).

- [x] T003 [P] [US1] RED: WebApp static-UI routes + bearer-token gate tests — create `tests/unit/test_web_app_kb.py` (seed with the scaffold tests; KB tests join in T007/T009). Using the 003 `tests/unit/test_web_server.py` harness pattern: build a migrated v3 state DB in `tmp_path`, construct the WebApp on `127.0.0.1:0` via `digital_twins.web.app.build_web_app`/`serve`, poll for readiness, drive requests with `http.client`. Assert: `GET /` returns `index.html` with `Content-Type: text/html`; `GET /static/style.css` returns 200; a path-traversal request (`/static/../knobs.py`) is rejected; every `/api/*` endpoint except `/api/auth/*` returns 401 without an `Authorization: Bearer` token; a `?token=` query fallback is accepted. Test fails now because `web/app.py` does not exist (RED).
- [x] T004 [US1] GREEN: web/app.py scaffold with static routes + auth gate — create `digital_twins/web/app.py` (C-2, R6=(a): a NEW module; `server.py` is NOT rewritten): a `WebApp` class (stdlib `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`, the 003 `web/server.py` model — no new web framework) plus `build_web_app(db, cfg, ...)` and `serve()` helpers. Route `/` → `web/static/index.html`, `/static/*` → `web/static/` assets (path-traversal-safe: reject `..` and absolute paths), `/api/*` → REST handler dispatch. Every `/api/*` route except `/api/auth/*` first verifies the session token via `digital_twins.auth` (the same helper 003's auth_checker uses): `Authorization: Bearer <token>`; `?token=` query fallback; absent/expired/revoked → 401, fail-closed (FR-017). T003 blocks T004.

---

## Phase 3: Auth Surface (US1, reuse 003)

**Purpose**: `/api/auth/*` + `/api/me` — the sign-in journey from a second machine (SC-001).

- [x] T005 [P] [US1] RED: /api/auth/* signup-signin-signout + /api/me tests — extend `tests/unit/test_web_app_auth.py` (new file). Red tests (SC-001): `POST /api/auth/signup` first account → 200 `{email, role: 'admin', created: true}`, second → role `reader`; duplicate email → 409 `{"error": "account already exists"}`; blank email/password → 400. `POST /api/auth/signin` valid → 200 `{session_token, expires_at}` (pbkdf2 hash in `sessions` table); wrong password → 401, no token. `POST /api/auth/signout` → 200 `{revoked: true}` and the same token then 401s on every `/api/*` call; signout with no/invalid token → 401. `GET /api/me` with a valid token → 200 `{email, role, point_count}` (`point_count` 0 on empty collection). Test fails now (RED).
- [x] T006 [US1] GREEN: /api/auth/* + /api/me wired via auth + state — implement in `web/app.py`: `/api/auth/signup`, `/api/auth/signin`, `/api/auth/signout` (FR-004, `contracts/web-api.md`) by delegating to `web/server.py`'s existing 003 credential handlers and `digital_twins.auth` (create account / authenticate / create_session / revoke_session) + `digital_twins.state` (accounts/sessions tables) — no duplicated auth logic (R2). First-ever account resolves to role `admin`, else `reader` (003 R7/C-4, open sign-up Q8, no OAuth/email-verification/admin-gating). `/api/auth/signout` calls `auth.revoke_session`. `GET /api/me` returns `{email, role, point_count}` (FR-003), owner-scoped via the caller's `owner_tag`. Round-trip risk check: a token 003's auth_checker accepts must be accepted here too. T005 blocks T006.

---

## Phase 4: KB Read Surface (US2/US3, Qdrant)

**Purpose**: Read-only KB surface — point counts and owner-scoped vector search.

- [x] T007 [P] [US2] RED: /api/kb/points count + owner_count + sample tests — extend `tests/unit/test_web_app_kb.py`. Red tests (FR-005): with an in-process or monkeypatched `qdrant_client` (C-5 — no live Qdrant), `GET /api/kb/points?limit=N` returns 200 `{count, owner_count, sample}`: `count` = `personal_kb` collection-wide point count, `owner_count` = points stamped with the caller's `owner_tag` (N when N are stamped), `source_count` when `?source=` is given, `sample` = up to `limit` rows (default 5, cap 100) each with `source`/`source_url`/`chunk_index`/`text`, most recent first; empty collection → `count` 0, `sample` [] (not an error). `?source=` restricts count+sample to that source. A monkeypatched client that raises → 502/503 `{"error": "qdrant unavailable: <hint>"}` with no crash/traceback. No token → 401. Test fails now (RED).
- [x] T008 [US2] GREEN: /api/kb/points reading Qdrant via config layer — implement `GET /api/kb/points` in `web/app.py` (C-4): open a `QdrantClient` from `qdrant.url`/`qdrant.api_key` via the config layer at runtime (no host defaults), read the `personal_kb` collection (the health.QDRANT_COLLECTION name); compute collection-wide count, owner_count via an owner_tag filter on the caller's tag, and a scroll/search of up to `limit` points projecting `source`/`source_url`/`chunk_index`/`text`. Optional `?source=` restricts count+sample to that source. Wrap all Qdrant access: unreachable/failed → 502/503 with a remediation hint, never a traceback. T007 blocks T008.
- [x] T009 [P] [US3] RED: /api/kb/search owner-scoped vector search tests — extend `tests/unit/test_web_app_kb.py`. Red tests (FR-006): `POST /api/kb/search` `{query, limit}` against a populated in-process/monkeypatched Qdrant returns 200 `{results: [{score, source_url, text, source, chunk_index}]}` — top-N (`limit` default 5, cap 100) sorted by descending score, owner-scoped to the caller's `owner_tag` (results only from the caller's points, not the whole collection). Blank or missing query → 400 `{"error": "query must be a non-empty string"}`. No/invalid token → 401; Qdrant down → 502/503 with hint. Test fails now (RED).
- [x] T010 [US3] GREEN: /api/kb/search via embedding + Qdrant search — implement `POST /api/kb/search` in `web/app.py` (C-4): validate the query (blank → 400 with the exact contract error), embed the query reusing `digital_twins.ingest.embedding`, run a vector search on `personal_kb` with an owner_tag filter for the caller, and return the top-N as `{score, source_url, text, source, chunk_index}` descending by score. Same Qdrant client construction + 502/503 wrapping as `/api/kb/points`. No separate search engine or parallel search logic — the same query path an MCP `kb_search` client would use. T009 blocks T010.

---

## Phase 5: Ingestion + Audit (US4/US5, reuse pipeline)

**Purpose**: Trigger ingestion through `digital_twins.ingest.pipeline` (the same path as `run --once` and MCP `kb_ingest`) with `trigger="web"`, and expose per-user audit rows (NFR-16).

- [x] T011 [P] [US4] RED: /api/ingest/run trigger=web, role gate, dedup parity — create `tests/unit/test_web_app_ingest.py`. Red tests (SC-002/SC-004, FR-007..FR-009). With a source enabled and prerequisites present: `POST /api/ingest/run` `{source: 'fs'}` → `run_pipeline` is called with `trigger='web'` and `scheduled_by=<caller-email>`; the `audit_runs` row is written with `trigger='web'`; response is the run summary `{run_id, status, counts, points}`. Dedup parity (NFR-1/NFR-14): ingest the same content via `run --once` (or an equivalent direct pipeline call) then via `/api/ingest/run` for the same source+window and assert the Qdrant point count is unchanged (one record, not N). Reader-role caller → 403 `{code: 'permission_denied'}` naming the missing `trigger_run` capability AND no audit row is written on refusal. Disabled source → 400 `{"error": "source '<name>' is not enabled"}`; unknown source → 400 `{"error": "unknown source '<name>'"}`; `'all'`/`{}` with none enabled → 400 `{"error": "no sources enabled"}`; enabled source missing prerequisites → 409/500 naming the missing prerequisite(s) and a `'failed'` audit row is written. No token → 401. Test fails now (RED).
- [x] T012 [US4] GREEN: /api/ingest/run through ingest.pipeline run_pipeline — implement `POST /api/ingest/run` in `web/app.py` (R3/C-6): before any pipeline work, check `digital_twins.accounts.require_capability(caller, 'trigger_run')` — reader → 403 `permission_denied` with the missing capability named, no audit row. Validate the source (disabled → 400 `'not enabled'`; unknown → 400 mirroring `run --once`'s UnknownSourceError; `'all'` or `{}` → run all enabled, none enabled → 400). Then call `digital_twins.ingest.pipeline.run_pipeline(db, cfg, source_names=[...], qdrant=..., neo4j=..., embedder=..., trigger='web', scheduled_by=<caller>, owner=<caller owner_tag>)` — the SAME code path as `run --once` and MCP `kb_ingest`, no separate ingestion logic; missing prerequisites → 409/500 naming them and a `'failed'` audit row. Points are owner-stamped with the caller's `owner_tag` (003 R6) and dedup'd so NFR-1/NFR-14 holds. T011 blocks T012.
- [x] T013 [P] [US5] RED: /api/audit/recent per-user scoping tests — create `tests/unit/test_web_app_audit.py`. Red tests (SC-005, FR-010, NFR-16): seed `audit_runs` with rows for alice and bob (including a `trigger='web'` row). Non-admin alice `GET /api/audit/recent?limit=10` → only rows where `scheduled_by=alice`. Admin `GET /api/audit/recent` → all users' rows (documented admin privilege). Rows in the 001/002/004 audit shape: `run_id`, `started_at`, `completed_at`, `status`, `trigger`, `scheduled_by`, per-source counts; most recent first; `limit` default 10, cap 100. A run just triggered via `/api/ingest/run` is visible immediately. No/invalid token → 401. Test fails now (RED).
- [x] T014 [US5] GREEN: /api/audit/recent reading audit_runs per user — implement `GET /api/audit/recent` in `web/app.py`: read `audit_runs` from the 001/003 SQLite state (unchanged schema, no new migration). Non-admin callers get only rows where `scheduled_by` equals their email (NFR-16 — a user lists only their own runs); an admin gets all rows. Return the last N (`limit` default 10, cap 100) most-recent-first in the 001/002/004 audit record shape. T013 blocks T014.

---

## Phase 6: Chat Surface (US6, C-1/R8)

**Purpose**: The chat **surface** only — authenticated, owner-scoped, audit-worthy; 501 until an LLM endpoint is configured (full RAG generation is a follow-up slice, C-1).

- [x] T015 [P] [US6] RED: /api/kb/chat 501 surface + validation tests — create `tests/unit/test_web_app_chat.py`. Red tests (FR-011, C-1/R8): with no `llm.endpoint` configured, `POST /api/kb/chat` `{query}` → 501 `{"code": "not_implemented", "remediation": "set llm.endpoint / llm.model to enable chat (006 ships the surface only; R8)"}` — a clean 501, not a crash and not a 401; the endpoint is auth-checked and owner-scoped (a valid session token is required; the caller's owner scope is established before the 501). Blank query → 400 `{"error": "query must be a non-empty string"}`. No/invalid token → 401. Assert the endpoint does NOT attempt LLM generation in 006. Test fails now (RED).
- [x] T016 [US6] GREEN: /api/kb/chat surface returning 501 until LLM — implement `POST /api/kb/chat` in `web/app.py` as the chat surface only (C-1): session-token auth, owner-scoping established, input validation (blank query → 400), then read `llm.endpoint`/`llm.model` from the config layer — when unset, return 501 `not_implemented` with the remediation hint naming `llm.endpoint`/`llm.model` (mirrors 004's BR-10 stub pattern). No RAG generation in this slice; the endpoint contract (auth, scoping, audit-worthiness, error shape) is stable so a follow-up slice fills generation without re-plumbing. T015 blocks T016.

---

## Phase 7: CLI + UI + Guards (US1)

**Purpose**: The `digital-twins web` subcommand, the static UI, and the portability guard extension.

- [x] T017 [P] [US1] RED: digital-twins web subcommand tests — create `tests/unit/test_web_cli.py`. Red tests (FR-013, C-6): the new `digital-twins web` click subcommand binds to `web.bind`/`web.port` (honoring the knobs, not hard-coding an address — e.g. override `web.bind`/`web.port` via config/env in a tmp state dir and assert the socket binds exactly there), and prints the listening URL in the form `web: listening on http://<bind>:<port> (UI: http://<bind>:<port>/)`. It reuses `cli.pre_command` (config load + migrations) and does NOT start the scheduler loop and does NOT write the scheduler pidfile (a separate surface from `serve`; assert the scheduler is not invoked). Test fails now (RED).
- [ ] T018 [US1] GREEN: add digital-twins web click subcommand — modify `digital_twins/cli.py` (click; `__main__.py` unchanged — reachable via `digital-twins web`): add the `web` subcommand — run `cli.pre_command` (load config, complete migrations), build the WebApp on `web.bind`/`web.port` via `web.app.build_web_app`, print the listening URL with the configured bind, and serve (`ThreadingHTTPServer`). It serves only the UI + API — it must not drag in the scheduler lifecycle (no scheduler loop, no pidfile). Host-neutral. T017 blocks T018.
- [ ] T019 [P] [US1] RED: static UI pages + assets-present + fetch-target tests — create `tests/integration/test_web_app.py`. Red tests (SC-006, FR-015): against the live WebApp on `127.0.0.1:0`, `GET /` serves the static `index.html` (`Content-Type: text/html`) containing: a sign-in form, a sign-up form (first-time), and a KB panel showing the point count, a search-query field, a 'Trigger ingestion' control (per-source or run-all-enabled), and the last run's status/audit row (NFR-16). `GET /static/style.css` → 200. The page's `fetch()` targets are exactly the `/api/*` paths (`/api/auth/signup`, `/api/auth/signin`, `/api/auth/signout`, `/api/me`, `/api/kb/points`, `/api/kb/search`, `/api/ingest/run`, `/api/audit/recent`, `/api/kb/chat`) — no parallel client-side logic (SC-006). The test fails now because the static assets do not exist yet (RED). This also seeds the end-to-end scenario assertions (sign up → sign in → /api/me → kb/points → kb/search → ingest/run → audit/recent) that T023 completes.
- [ ] T020 [US1] GREEN: static index.html + style.css driving /api/* — create `digital_twins/web/static/index.html` (single page: sign-in form, sign-up form for first-time, KB panel with point count, search box, Trigger-ingestion button (per-source or run-all-enabled), and the last-audit-row display) + `digital_twins/web/static/style.css` (minimal styling). Plain HTML only — static HTML, no build step, no SPA framework, no node_modules/npm (R4); plain `fetch()` to `/api/*` + vanilla DOM. Host-neutral: relative `/api/*` paths only, localhost placeholders only in docs (NFR-13). T019 blocks T020.
- [ ] T021 [US1] Extend test_portability.py SHIPPED to the 006 web artifacts — modify `tests/integration/test_portability.py` (C-7/FR-016/NFR-13): add the new 006 artifacts to the SHIPPED scan: `digital_twins/web/` (the Python `app.py`/`server.py`/`__init__.py`) and the non-Python static assets via a `SHIPPED_NON_PY` entry for `web/static/index.html` and `web/static/style.css`, so a host path/username leaked into the HTML/CSS fails the guard. Re-run the existing `HOST_PATTERNS` / `INTERPRETER_PIN` / `LITERAL_USERNAMES` patterns over them — no new pattern needed. No red step (docs/config): verified by this guard run; confirm `pytest tests/integration/test_portability.py` is green. T020 blocks T021.

---

## Phase 8: Standing Guards (cross-cutting)

**Purpose**: Confirm the two standing guards stay green with the new web knobs in lock-step.

- [ ] T022 [cross-cutting] Confirm test_knob_docs.py green for new web.* knobs — verify constitution IV lock-step (C-8) for the three new web knobs: run `pytest tests/unit/test_knob_docs.py` and confirm 100% coverage of `web.bind`/`web.port`/`web.base_url` across all four surfaces (`digital_twins/config/knobs.py` registry, `config.example.yml`, `.env.example`, `docs/configuration.md`) — the standing guard enforces registry ↔ example-files ↔ docs, so the new knobs pass only when all four are updated consistently. No new test logic needed. No red step (docs/config); record the green result in the SDD ledger.

---

## Phase 9: Polish & Cross-Cutting Concerns (cross-cutting)

**Purpose**: Whole-feature validation, quickstart, and release readiness.

- [ ] T023 [P] [cross-cutting] END-TO-END GREEN: full sign-up-through-audit integration test — complete `tests/integration/test_web_app.py` (the 003 harness, C-5): on the live WebApp on `127.0.0.1:0`, drive the full journey — sign up (first → admin, second → reader) → sign in → `/api/me` → `/api/kb/points` → `/api/kb/search` → `/api/ingest/run` → `/api/audit/recent` — over `http.client`/`urllib` with a migrated v3 DB in `tmp_path` and an in-process/monkeypatched Qdrant. Assert SC-001 (second-machine sign-in, no CLI), SC-002 (dedup parity: the web-triggered run's point count is unchanged vs `run --once` and the `trigger='web'` + `scheduled_by=<caller>` audit row is present), SC-004 (401/403 fail-closed), SC-005 (per-user audit), and SC-006 (static UI drives `/api/*` only). The red assertions from T019 become green here. T019 blocks T023; depends on T006/T008/T010/T012/T014.
- [ ] T024 [P] [cross-cutting] Write host-neutral specs/006-web-app/quickstart.md — describe the 'install → init → serve → open browser on another machine → sign in → query → trigger ingestion' flow, matching the shipped behavior (R5/R7: widen `web.bind=0.0.0.0` / `KB_WEB__BIND=0.0.0.0`, `digital-twins web` on port 8767, email+password sign-in, KB panel count/search/trigger, last-audit-row). Note the chat surface is 501 `not_implemented` until an LLM endpoint is set (C-1/R8). Host-neutral throughout (NFR-13): no host path, username, install location, or `python3.N` pin — verified by the Phase-8 portability guard once the spec file is added to the scan scope. No red step (docs).
- [ ] T025 [cross-cutting] Full suite green + ledger entry (version stays 0.5.0) — run the entire `pytest` suite and confirm: `tests/unit/test_web_config_knobs.py` + `test_web_app_auth`/`kb`/`ingest`/`audit`/`chat` + `test_web_cli.py` green; `tests/integration/test_web_app.py` green (SC-001/002/004/005/006); `tests/unit/test_knob_docs.py` green (100% coverage of the new web.* knobs — SC-003/C-8); `tests/integration/test_portability.py` green (006 web artifacts + static assets host-neutral — NFR-13); all 001–005 tests still green (total ≥ 682 baseline + new tests). Confirm the version stays 0.5.0 during the loop (no version-bump task — the 0.5.0→0.6.0 bump is a merge-time task, R15 from 005). Record all results + the version-holds note in the SDD ledger (`.superpowers/sdd/006-web-app/progress.md`). No red step (verification/ledger).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately.
- **Phase 2 (Scaffold)**: Depends on Phase 1 (knobs exist so the app can read them).
- **Phase 3 (Auth Surface)**: Depends on Phase 2 (WebApp class + bearer gate).
- **Phase 4 (KB Read)**: Depends on Phase 2 (WebApp + Qdrant client). US2/US3 parallel after Phase 2.
- **Phase 5 (Ingestion + Audit)**: Depends on Phase 3 (caller identity) + Phase 2 (WebApp). US4/US5.
- **Phase 6 (Chat Surface)**: Depends on Phase 2 (WebApp + auth gate). US6, parallel with Phases 4–5.
- **Phase 7 (CLI + UI + Guards)**: Depends on Phases 2–6 (API surface stable before the UI fetches it). US1.
- **Phase 8 (Standing Guards)**: Depends on Phase 1 (knobs in lock-step).
- **Phase 9 (Polish)**: Depends on all prior phases.

### User Story Dependencies

- **US1 (P1)**: Phase 1 → 2 → 3 → 7 (CLI + UI). The scaffold + auth + CLI + UI form the sign-in journey.
- **US2 (P1)**: Phase 4 (kb/points) — parallel after Phase 2.
- **US3 (P1)**: Phase 4 (kb/search) — parallel after Phase 2.
- **US4 (P1)**: Phase 5 (ingest/run) — after Phase 3 (caller identity).
- **US5 (P2)**: Phase 5 (audit/recent) — after Phase 3.
- **US6 (P3)**: Phase 6 (chat surface) — after Phase 2.

### Within Each User Story

- Tests MUST be written and FAIL before implementation (Constitution III).
- Contracts/models before services; services before CLI wiring.
- Story complete (checkpoint green) before the next priority.

### Parallel Opportunities

- Phase 1: T001 [P] (the red test is self-contained).
- Phase 4: T007/T009 [P] together (both red tests in `test_web_app_kb.py`, disjoint test functions).
- Phase 5: T011 [P] (red test self-contained).
- Phase 6: T015 [P] (red test self-contained).
- Phase 7: T017/T019 [P] together (disjoint test files).
- Phase 9: T023/T024 [P] together (disjoint files).

---

## Parallel Example: Phase 4 (KB Read Surface)

```text
# Launch red tests together (disjoint test functions in the same file):
Task: "Write /api/kb/points red tests in tests/unit/test_web_app_kb.py (T007)"
Task: "Write /api/kb/search red tests in tests/unit/test_web_app_kb.py (T009)"

# After the WebApp scaffold (T004) lands, the green implementations can proceed:
Task: "Implement /api/kb/points in web/app.py (T008)"
Task: "Implement /api/kb/search in web/app.py (T010)"
```

---

## Implementation Strategy

### MVP First (US1 Only)

1. Complete Phase 1: Setup (web.* knobs in lock-step)
2. Complete Phase 2: WebApp scaffold (static routes + bearer gate)
3. Complete Phase 3: Auth surface (signup/signin/signout + /api/me)
4. Complete Phase 7 (CLI + UI subset): `digital-twins web` subcommand + static index.html
5. **STOP and VALIDATE**: SC-001 — sign in from a second machine, no CLI required.

### Incremental Delivery

1. Phase 1 + 2 → foundation ready (knobs + WebApp)
2. US1 → sign-in journey (auth surface + CLI + UI) → MVP web tool
3. US2 + US3 → KB read surface (points + search) → query from the browser
4. US4 + US5 → ingestion + audit (trigger via web, per-user history)
5. US6 → chat surface (501 until LLM) → stable contract for the follow-up slice
6. Phase 8 + 9 → standing guards green, quickstart, full suite, ledger

### Handoff Note (superpowers)

This task list is the execution plan for the superpowers phase: run `subagent-driven-development` (or `executing-plans`) over it, one task per TDD cycle (red → green → commit), stopping at each checkpoint for `verification-before-completion`.

---

## Notes

- [P] tasks = different files / disjoint test functions, no dependencies on unfinished tasks
- [Story] labels map every story-phase task to US1–US6 for traceability
- Commit after each task or logical group
- Stop at any checkpoint and validate the story independently before moving on
- `test_portability.py` (T021) and `test_knob_docs.py` (T022) are standing guards: they must stay green through every later phase
- Version stays 0.5.0 during the SDD loop; the 0.5.0→0.6.0 bump is a merge-time task (R15 from 005)
- `web/server.py` is NOT modified (R6=(a): the new `web/app.py` wraps/extends it)
- `web/__init__.py` and `__main__.py` are unchanged
- `run_pipeline` is called with the existing `trigger`/`scheduled_by`/`owner` kwargs (003 supports all three)
