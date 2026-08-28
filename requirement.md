---
name: digital-twins-portable-brd
description: Business Requirements for promoting the Daily KB Session Ingest cron job into a portable, community-sharable KB ingestion tool — deployable package, environment decoupling, generic MCP agent access, and multi-user sign-up/sign-in.
tags: [knowledge, requirements, packaging, mcp, scheduler, ingestion, auth, portability]
---

# Digital Twins — Portable, Multi-User Edition

> **Status:** Draft v0.2 (2026-08-29) — owner decisions Q1/Q2/Q3/Q4/Q5/Q6/Q7/Q8/Q9/Q10 all locked in.
> **Owner:** project owner
> **Base:** Extends `docs/business-requirements.md` (BR-1..BR-10, NFR-1..11).
> This document defines NEW requirements (BR-11) for turning the host-specific
> *Daily KB Session Ingest* cron job into a deployable, environment-portable,
> multi-user tool. All existing BR/NFR continue to apply unchanged.
> **Scope:** What the system MUST do when deployed as a package any user can
> install and run in their own environment. Not implementation.

---

## 1. Current State (baseline being promoted)

The current **Daily KB Session Ingest** job (Hermes cron `e4735cf2a2f2`,
`0 3 * * *`) is a 7-step agent-driven pipeline hard-wired to one host:

| # | Step | Source | Hard-coupling to this host |
|---|------|--------|----------------------------|
| 0 | Pi backfill (one-time) | `~/.pi/agent/sessions` | Host path; one-shot flag `~/.hermes/kb_pi_backfill_done.flag` |
| 1 | Hermes sessions (24h) | `~/.hermes/profiles/<active>/state.db` | Hermes install path + active profile |
| 2 | Pi sessions (24h) | `~/.pi/agent/sessions` | pi install path; `source_url` prefix `pi:` |
| 3 | DSH sessions (24h) | `~/.dsh/sessions` | DSH install path; `source_url` prefix `dsh:` |
| 4 | Paperclip chat | Paperclip DB (PG) | Paperclip config + PG credentials; writes to Qdrant **and** mem0_terry |
| 5 | Yahoo mail (IMAP, unseent, cap 200) | IMAP | `YMAIL_APP_PASSWORD` in `.env`; Yahoo-specific auth rules |
| 6 | Gmail mail (IMAP, unseent, cap 200) | IMAP | `GMAIL_APP_PASSWORD` in `.env` |
| 7 | Report | — | Prints counts to agent transcript |

Coupling summary:
- **Environment**: scripts live in `~/.hermes/skills/hermes/personal-kb/scripts/`
  (outside the project repo); Python pinned to `python3.12`; `CUDA_VISIBLE_DEVICES=""`
  enforced; all source paths are absolute home-dir paths.
- **Scheduling**: driven by a Hermes cron prompt (agent re-runs shell commands
  daily). No standalone scheduler; no user-facing scheduling UI; no per-user
  run history.
- **Agent access**: MCP exists (BR-10) but auth is service-token + account;
  there is no generic sign-up path for arbitrary agents/users, and the MCP
  tool set does not expose *scheduling* or *run reporting* — only search/chat/ingest/health.
- **Multi-user**: single `.kbstate/users.db`; one operator. No per-user
  workspace, per-user run history, or per-user channel config.

**Promotion goal:** the same pipeline becomes a **package** (`digital-twins`)
any user can install, point at their own Qdrant/Neo4j/LLM, run on their own
schedule, reach from any MCP-capable agent, and sign in to as a user with
their own history and config.

---

## 2. New Functional Requirements

### BR-11 — Portable, multi-user KB ingestion scheduler

The system SHALL package the ingestion pipeline as an independently
installable, environment-portable, multi-user, MCP-reachable tool.

#### BR-11.1 — Deployable package

- **BR-11.1.1** The tool SHALL be published as an installable unit under a
  stable name (e.g. `digital-twins`) so that `pip install digital-twins` (or the
  equivalent for the chosen distribution channel) yields a working binary.

- **BR-11.1.2** The package SHALL declare all runtime dependencies
  (Python version, qdrant-client, neo4j, sentence-transformers, embedding
  model, any IMAP / session-DB libraries) in a machine-readable manifest
  (`pyproject.toml` / equivalent). No implicit dependencies on files outside
  the package.
- **BR-11.1.3** The package SHALL bundle a first-run initializer
  (`digital-twins init` or equivalent) that, on a clean host:
  - prompts for (or reads from `.env` / config) Qdrant, Neo4j, and LLM
    endpoints,
  - creates the local state directory (`.kbstate/` or equivalent),
  - creates the account database,
  - writes a starter config with sane defaults,
  - runs `validate` (BR-3.6) and reports health.
- **BR-11.1.4** The package SHALL be self-contained: no scripts may live
  outside the installed package at runtime. The host-specific
  `~/.hermes/skills/hermes/personal-kb/scripts/*.py` files SHALL be absorbed
  into the package (or reimplemented inside it) so that a fresh install works
  with zero host-specific files.
- **BR-11.1.5** The package SHALL provide a machine-readable version
  (`digital-twins --version`) and a changelog (`CHANGELOG.md`) so users can
  track upgrades.
- **BR-11.1.6** A Docker image (or `docker-compose.yml` for the full stack:
  Qdrant + Neo4j + digital-twins + LLM + embedding model) SHALL be published
  so a user can run the whole thing with one `docker compose up`
  (Q7, DECIDED 2026-08-29: "docker image bundle all, but leave their
  endpoint configurable"). The image SHALL:
  - **bundle** the embedding model (BGE-small-en-v1.5) and an LLM runtime
    (SGLang or equivalent) so a user with no external LLM can run end-to-end
    from the container;
  - **pin** the embedding model and backend versions (no `:latest` floats
    for the embedding, which would silently change the vector space);
  - **expose** the Qdrant, Neo4j, and LLM endpoints as **configurable**
    (env vars / compose `environment` / `kb.yml` overrides) so a user MAY
    point the stack at their own external Qdrant / Neo4j / LLM instead of
    the bundled ones — e.g. `QDRANT_HOST=host.docker.internal` to reuse an
    existing local Qdrant, or `SGLANG_ENDPOINT=https://my-llm:8080` to use a
    remote LLM. The bundled backends are the default; external endpoints are
    an explicit opt-in.
  The compose file SHALL document both modes (bundled-only and
  bundled-with-external-endpoints) in the README.
- **BR-11.1.7** The package SHALL be published on **PyPI** under the name
  `digital-twins` (or a PyPI-namespace subproject, e.g. `digital-twins` under
  the owner's PyPI account). The project SHALL maintain a public repository
  (GitHub or equivalent) with the source, issues, and releases. PyPI is the
  primary distribution channel; a Docker image (BR-11.1.6) is a secondary,
  convenience channel for users who prefer containerised deploys.
- **BR-11.1.8** The package SHALL ship a **web application** (the existing
  admin web UI, BR-8) as a first-class surface so that any user — not just
  the host operator — can access the KB from a browser anywhere. The web
  app SHALL be reachable over a configurable bind address (default
  `127.0.0.1`, BR-8.8) and SHALL support the full BR-8/BR-11.4 surface
  (dashboard, query, chat, ingestion triggers, account management, run
  history). A user who installs `digital-twins` on their own host and wants
  to use it from a phone, laptop, or remote machine SHALL be able to point
  a browser at the web app and sign in (BR-11.4) — no CLI required.

#### BR-11.2 — Environment decoupling

- **BR-11.2.1** The tool SHALL NOT assume any host-specific path. Every
  source location (Hermes state DB, pi sessions dir, DSH sessions dir,
  Paperclip DB, mail INBOX) SHALL be a **named, configurable source**
  resolved through the same config layer as BR-2/BR-3:
  1. process env / `.env`
  2. `kb.local.yml` (machine-specific, untracked)
  3. `kb.yml` (committed defaults)
  4. built-in defaults (all disabled by default on a fresh install).
- **BR-11.2.2** Each source SHALL have a **capability declaration**
  (which agent-runtime it needs, which credential it needs, which
  `source_url` prefix / payload tags it stamps). Enabling a source without
  its prerequisite (e.g. Hermes source without a readable state DB) MUST fail
  fast with a clear error naming the missing prerequisite — not silently
  ingest 0 items.
- **BR-11.2.3** The embedding model, LLM endpoint, Qdrant/Neo4j endpoints,
  and chunking parameters are governed by BR-3 (already configurable); this
  BR adds the requirement that the package's **defaults are safe for a fresh
  host** (no host baked in, all endpoints unset → `init` prompts for them).
- **BR-11.2.4** The package SHALL ship a `.env.example` and a `config.example.yml`
  that document every knob a user may need to set in their own environment,
  grouped by source (Qdrant / Neo4j / LLM / Hermes / pi / DSH / Paperclip /
  email). No knob may exist that is not documented.
- **BR-11.2.5** The tool SHALL detect and refuse to run if the configured
  embedding model does not match the collection's existing vector dimension
  (NFR-2). A mismatch MUST produce a hard error with a remediation message
  (re-embed, or point at a new collection) — never a silent dimension mismatch.
- **BR-11.2.6** Python version SHALL be declared as a package requirement
  (`requires-python >= 3.11` or the version the embedding model supports);
  the host's `python3.12` pin SHALL NOT be a hidden assumption. NFR-4 is
  relaxed to "the package declares a supported Python range; the host must
  satisfy it."
- **BR-11.2.7** **Default source set on a fresh install: all disabled.**
  (Q5, DECIDED 2026-08-29: "disabled on fresh install, but they can be
  configured and new sources can be added later.") A clean install SHALL
  start with every source (Hermes, pi, DSH, Paperclip, Gmail, Yahoo, and
  any future channel) **disabled**. The user SHALL be able to enable any
  source via `kb.yml` / `kb.local.yml` / the web UI / MCP, and SHALL be able
  to add **new sources** (custom channels) without a package update,
  following the BR-1.4/BR-2 channel-extension pattern. The first-run
  `init` (BR-11.1.3) SHALL present the available sources and let the user
  enable whichever they have credentials for — it SHALL NOT assume any
  source is configured.

#### BR-11.3 — Scheduled & on-demand runs (scheduler surface)

- **BR-11.3.1** The tool SHALL run as **both** a standalone scheduler and a
  one-shot CLI (Q6, DECIDED 2026-08-29: "both"):
  - `digital-twins serve` — a long-running process that fires runs on a
    cron-like schedule, manages its own queue, and exposes a status endpoint
    (BR-11.3.6). Suitable for `systemd` / `supervisord` / a Docker container.
  - `digital-twins run --once` — a stateless one-shot invocation suitable for
    the host's `cron` / `systemd` / a CI trigger. Performs a single run and
    exits.
  The two forms SHALL produce identical ingestion results (same pipeline,
  same deterministic IDs, same audit record shape). A user MAY use either or
  both; the scheduler state (`.kbstate/`) is shared so a `run --once` fired
  by host cron and a `serve`-scheduled run never duplicate points (NFR-1,
  NFR-14).
- **BR-11.3.2** Run schedules SHALL be **per-user, per-source** and editable
  via the API/UI and MCP: e.g. "user alice runs the hermes source daily at
  03:00, and the gmail source daily at 03:30." The current single 03:00
  all-sources schedule is a default, not a hard-coded value.
- **BR-11.3.3** Each scheduled run SHALL produce an audit record with the
  BR-5.3 fields **plus**: `run_id`, `scheduled_by` (user or `system`),
  `trigger` (`schedule` | `manual` | `mcp` | `api`), `per-source` counts,
  and `started_at`/`completed_at`. Records SHALL be queryable by the
  originating user (BR-11.4).
- **BR-11.3.4** The scheduler SHALL be **resumable** (NFR-9): an interrupted
  run (process killed, host rebooted) MUST resume from the last committed
  item, not restart from scratch. Per-source high-water marks SHALL persist
  in the state store.
- **BR-11.3.5** The scheduler SHALL respect per-run caps and timeouts
  (the current 200-email / 1500s timeout is a per-source knob, not a
  global constant). A source may be paused, re-enabled, or re-capped without
  restarting the scheduler.
- **BR-11.3.6** The scheduler SHALL expose a **health/status** endpoint
  (mirroring BR-3.6) reporting: next fire time per schedule, last run status
  per source, and current queue depth. This is the surface an agent or UI
  polls to answer "is the ingestion healthy?"
- **BR-11.3.7** **Scheduling UX: keep it simple** (Q10, DECIDED 2026-08-29:
  "keep it simpler for users"). The package SHALL NOT ship a full cron
  expression parser. Instead:
  - **Preset cadences:** the UI / MCP / CLI SHALL offer a small set of named
    presets — `daily`, `hourly`, `weekly`, `monthly`, and `every-N-hours`
    (where N is a user-supplied integer). A preset expands to a concrete
    fire-time internally; the user never writes a cron expression.
  - **Optional host cron:** for users who *do* want cron, the package SHALL
    document a one-line host-cron snippet (`0 3 * * * digital-twins run
    --once --as <user>`) as an **alternative** to `digital-twins serve`, but
    the package itself does not parse or store cron strings.
  - **Custom fire times (follow-up):** arbitrary cron / recurrence rules
    (e.g. "every weekday at 09:00") are a **follow-up** feature; v1 covers
    the presets + host-cron escape hatch.

#### BR-11.4 — Multi-user sign-up & sign-in

- **BR-11.4.1** **Sign-up (simple, v1):** (Q8, DECIDED 2026-08-29: "keep it
  simple at beginning, just user email as username and password.") A user
  SHALL create an account with their **email address as the username** and a
  password — the same simple model as BR-9.1, no OAuth, no email-verification
  step, no admin-gating in v1. Sign-up SHALL be open on a fresh install
  (the `/signup` surface from BR-9); the first account created SHALL become
  admin (BR-9.1), subsequent sign-ups SHALL be reader by default
  (BR-9.5). Email-verification, OAuth, and admin-gated sign-up are
  **follow-up** features, not v1.
- **BR-11.4.2** Each user SHALL have:
  - their own **channel/source config overrides** (which sources they
    schedule, their per-source caps, their tags), stored per-user without
    affecting other users' runs;
  - their own **run history** (the audit records from BR-11.3.3, filtered to
    runs they triggered or were scheduled under their account);
  - their own **API / MCP credentials** (a personal token in addition to the
    shared service token).
- **BR-11.4.3** **Data isolation (v1):** all users share the **same KB data
  store** (single Qdrant collection + Neo4j graph, per BRD §1 out-of-scope
  "multi-tenant data isolation"). Per-user **tags** SHALL be stamped on
  ingested points so a user MAY query "only what I ingested" (post-filter on
  `owner == <user>` or `tag == <user>-ingest`). **Hard per-user isolation**
  (each user gets their own Qdrant collection + Neo4j subgraph) is a
  **follow-up release** (Q2, DECIDED 2026-08-29: "hard per-user later").
- **BR-11.4.4** **Roles:** the system SHALL support three roles (Q3,
  DECIDED 2026-08-29: "add scheduler role"), extending BR-9.5:
  - **admin** — all surfaces: configuration, account management, ingestion,
    schedules, query. The last remaining admin MUST NOT be deleted or
    demoted (BR-9.5).
  - **scheduler** — may manage schedules (create / update / delete / list)
    and trigger runs (`kb_schedule_*`, `kb_schedule_run`, `kb_run_history`),
    but MUST NOT reconfigure system endpoints (Qdrant / Neo4j / LLM), manage
    accounts, or edit global channel config. A scheduler MAY view their own
    run history and trigger runs on their behalf.
  - **reader** — query / dashboard / chat only; mutating routes MUST be
    denied with 403 (BR-9.5).
  Role assignment is an admin action (BR-9.4/9.5). The `scheduler` role is
  the default for users who sign up specifically to manage ingestion
  schedules without full admin rights.
- **BR-11.4.5** **Sign-in surfaces:** a user's credentials SHALL be accepted
  on (a) the web UI, (b) the HTTP API, (c) the MCP server (BR-10.5), and
  (d) the scheduler CLI (`digital-twins run --as <user>`). Unauthenticated
  access to any of these is denied (BR-9.2).
- **BR-11.4.6** **First user:** on a fresh install, the first account created
  via `init` or `/setup` SHALL become admin (BR-9.1). Subsequent sign-ups
  SHALL be **reader** by default; promotion to admin is an admin action
  (BR-9.4/9.5).

#### BR-11.5 — MCP access for any agent

- **BR-11.5.1** The MCP server SHALL extend BR-10 with **scheduler tools** so
  any MCP-capable agent (not just the host's Hermes) can:
  - `kb_schedule_list` — list schedules and their next fire time;
  - `kb_schedule_create` / `kb_schedule_update` / `kb_schedule_delete` —
    manage schedules (admin or scheduler role);
  - `kb_schedule_run` — trigger a one-shot run now (admin or scheduler role);
  - `kb_run_history` — fetch audit records for a user/source/time range.
- **BR-11.5.5** **Access control (placeholder):** (Q9, DECIDED 2026-08-29:
  "any user, but put a placeholder for access control later.") In v1, any
  authenticated user MAY call the scheduler tools (`kb_schedule_*`,
  `kb_schedule_run`, `kb_run_history`); the tools SHALL operate on the
  caller's own schedules and run history by default (a user cannot list or
  trigger another user's schedules without admin/scheduler privilege). The
  **granular per-schedule ACL** (e.g. "user X may only trigger schedule Y,
  not create new ones") is a **follow-up** — the v1 schema SHALL reserve a
  `schedule.acl` field (default: `owner`) so the access-control model can be
  added later without a data migration.
- **BR-11.5.2** The MCP auth model SHALL remain BR-10.5 (account session or
  service token), but the **generic agent onboarding** path SHALL be a single
  documented line: an agent with a valid token may `kb_search`, `kb_chat`,
  and — if admin — `kb_ingest` and the new scheduler tools, with **no
  host-specific setup beyond the token**. The existing
  `docs/references/agent-guides.md` "Any MCP agent" section SHALL be the
  canonical onboarding doc and updated to cover the new tools.
- **BR-11.5.3** MCP scheduler-tool invocations SHALL be audited identically
  to API/UI/CLI runs (BR-4.6, BR-5.3), with `trigger: "mcp"` and
  `agent_kind` recorded (the existing `kb_ingest` `agent_kind` pattern).
- **BR-11.5.4** The MCP server SHALL be transport-agnostic (BR-10.6: stdio
  and HTTP/SSE). A community user running their own Qdrant+Neo4j+LLM SHALL be
  able to point any MCP client (Claude Desktop, Cursor, pi, DSH, Hermes, …)
  at their local `kb-mcp` with a token, and have the full tool set including
  scheduler tools.

#### BR-11.6 — Community packaging & documentation

- **BR-11.6.1** The package SHALL ship a **README.md** with: one-paragraph
  what-it-is, 5-minute quick start (install → init → first run), a
  configuration reference, and an "add a new source" how-to.
- **BR-11.6.2** The package SHALL ship a **LICENSE** file containing the
  **MIT License** text (Q4, DECIDED 2026-08-29). The copyright holder SHALL
  be the project owner. A `LICENSE` file at the repo root and a license
  declaration in `pyproject.toml` (`license = {text = "MIT"}` or the SPDX
  equivalent) SHALL both be present so `pip` / PyPI display the license
  correctly.
- **BR-11.6.3** The package SHALL have a **machine-readable issue/feature
  tracker surface** (GitHub issues or equivalent) and a **versioning policy**
  (semantic versioning; breaking config changes require a major bump +
  migration note).
- **BR-11.6.4** The package SHALL publish **changelog-driven release notes**
  and a **stable, documented config schema** so a user upgrading from v1 →
  v2 knows exactly what changed and how to migrate.

---

## 3. Non-Functional Requirements (additions)

| ID | Requirement |
|----|-------------|
| NFR-12 | **Portability** — a fresh host with Docker (or Python + the declared dependencies) and network access can install, init, and run a first ingestion run in < 30 minutes without reading source code. |
| NFR-13 | **Environment neutrality** — no host-specific path, username, or install location appears in the shipped code, config defaults, or docs. All such values are resolved through the config layer at runtime. |
| NFR-14 | **Idempotent scheduling** — firing the same schedule twice (e.g. clock drift + manual re-run) does not duplicate points (NFR-1 still applies; the scheduler is one more trigger under BR-4). |
| NFR-15 | **Upgrade safety** — an in-place upgrade (new package version over an existing install) MUST not lose state: `.kbstate/`, the account DB, and the config survive; a migration step (if schema changed) runs before the new code starts. |
| NFR-16 | **Per-user auditability** — every run is attributable to a user (or `system`); a user can list only their own runs; an admin can list all. |
| NFR-17 | **Credential scoping** — a user's personal token grants only that user's role; it MUST NOT grant access to another user's run history or config (NFR-5/11 extended to tokens). |

---

## 4. Acceptance Criteria (high-level)

- [ ] `pip install digital-twins` from **PyPI** on a clean host yields a
      `digital-twins` binary; `digital-twins init` completes a first-run setup
      (with **all sources disabled** by default — Q5) and `validate` reports
      Qdrant/Neo4j/LLM health.
- [ ] A user can enable/disable each source (Hermes, pi, DSH, Paperclip,
      Gmail, Yahoo) independently via config, with no code change, and a
      missing prerequisite fails fast with a clear error (BR-11.2.2). A user
      can also **add a new source** (custom channel) without a package update
      (BR-11.2.7, BR-1.4).
- [ ] The **web application** is reachable from a browser on a different
      machine (phone / laptop / remote) after the user widens the bind
      address; the user signs in with email + password (BR-11.1.8,
      BR-11.4.1) and can query / chat / trigger ingestion from the UI —
      no CLI required.
- [ ] The same content ingested via schedule, via `digital-twins run --once`,
      via MCP `kb_ingest`, and via the web UI yields **one** point, not four
      (NFR-1, NFR-14).
- [ ] Two users (alice, bob) with different schedules and different
      per-source caps run independently; each sees only their own run
      history; both share the same KB data store (BR-11.4.2/11.4.3).
- [ ] An external MCP agent (not the host's Hermes) authenticates with a
      token, calls `kb_search`, and — if admin — `kb_schedule_run`; the run
      is audited with `trigger: "mcp"` and the agent's identity (BR-11.5.1,
      BR-11.5.3).
- [ ] An interrupted run resumes from the last committed item, not from
      scratch (NFR-9, BR-11.3.4).
- [ ] Upgrading the package from v1 to v2 preserves `.kbstate/`, the account
      DB, and config; no data loss (NFR-15).
- [ ] A mismatch between the configured embedding model and the collection's
      vector dimension produces a hard error with a remediation message,
      never a silent mismatch (BR-11.2.5, NFR-2).
- [ ] The package ships a README, **MIT LICENSE**, CHANGELOG, and a documented
      config schema; the license is declared in `pyproject.toml` so PyPI
      displays it; a community user can follow the README to a working first
      run (BR-11.6, NFR-12, Q4).
- [ ] A user with the **scheduler** role can create / update / delete
      schedules and trigger runs, but a request to reconfigure system
      endpoints (Qdrant / Neo4j / LLM) or manage accounts is denied with 403
      (BR-11.4.4, Q3).
- [ ] `digital-twins serve` (long-running) and `digital-twins run --once`
      (one-shot) produce identical ingestion results for the same source +
      time window; running both against the same source does not duplicate
      points (BR-11.3.1, NFR-1, NFR-14, Q6).
- [ ] `docker compose up` from the published image yields a fully working
      stack (Qdrant + Neo4j + digital-twins + LLM + embedding model bundled);
      the user may override any endpoint (e.g. `QDRANT_HOST`, `SGLANG_ENDPOINT`)
      to point at an external backend without rebuilding the image
      (BR-11.1.6, Q7).
- [ ] A user can create a schedule using a named preset (`daily`, `hourly`,
      `weekly`, `monthly`, `every-N-hours`) without writing a cron expression;
      the UI / CLI / MCP all expose the presets (BR-11.3.7, Q10).
- [ ] An external MCP user (not admin) can call `kb_schedule_run` on their own
      schedule and `kb_run_history` for their own runs, but cannot list or
      trigger another user's schedules (BR-11.5.5, Q9).

---

## 5. Open Questions (need owner decision)

All ten questions are now **DECIDED** (2026-08-29). No open items remain;
the document is ready to drive implementation.

- **Q1 (BR-11.1.1):** ✅ **DECIDED 2026-08-29** — PyPI public, plus a web
  application surface so users can access the KB from anywhere (browser,
  phone, remote machine). Locked in BR-11.1.7 (PyPI + public repo) and
  BR-11.1.8 (web app as first-class surface).
- **Q2 (BR-11.4.3):** ✅ **DECIDED 2026-08-29** — Hard per-user isolation
  (separate Qdrant collection + Neo4j subgraph per user) is a **follow-up
  release**, not v1. v1 uses shared KB + per-user tags (soft isolation).
  Locked in BR-11.4.3.
- **Q3 (BR-11.4.4):** ✅ **DECIDED 2026-08-29** — Add a `scheduler` role
  between admin and reader. Locked in BR-11.4.4.
- **Q4 (BR-11.6.2):** ✅ **DECIDED 2026-08-29** — **MIT license.** The
  package SHALL be released under MIT so the community can fork/extend
  legally. Locked in BR-11.6.2.
- **Q5 (BR-11.2.7):** ✅ **DECIDED 2026-08-29** — All sources **disabled** on
  a fresh install; users configure the ones they have credentials for and
  can add new sources later. Locked in BR-11.2.7.
- **Q6 (BR-11.3.1):** ✅ **DECIDED 2026-08-29** — Both forms: a long-running
  `digital-twins serve` and a stateless `digital-twins run --once`. Locked in
  BR-11.3.1.
- **Q7 (BR-11.1.6):** ✅ **DECIDED 2026-08-29** — Docker image bundles all
  components (Qdrant + Neo4j + digital-twins + LLM + embedding model), but
  endpoints remain configurable so a user can point at external backends.
  Locked in BR-11.1.6.
- **Q8 (BR-11.4.1):** ✅ **DECIDED 2026-08-29** — Keep it simple: user email
  as username + password, open sign-up on a fresh install, no OAuth /
  email-verification / admin-gating in v1. Locked in BR-11.4.1.
- **Q9 (BR-11.5.5):** ✅ **DECIDED 2026-08-29** — Any authenticated user may
  call the scheduler tools in v1, scoped to their own schedules/run history
  by default. A `schedule.acl` field is reserved for granular per-schedule
  access control as a follow-up. Locked in BR-11.5.5.
- **Q10 (BR-11.3.7):** ✅ **DECIDED 2026-08-29** — Keep it simpler: no cron
  parser in the package. Offer named presets (`daily`, `hourly`, `weekly`,
  `monthly`, `every-N-hours`) plus a documented host-cron escape hatch
  (`digital-twins run --once`). Arbitrary cron / recurrence is a follow-up.
  Locked in BR-11.3.7.

---

## 6. Traceability to Existing BR/NFR

| New BR | Serves / extends |
|--------|------------------|
| BR-11.1 (package) | BR-4.4 (scheduled runs), BR-3.6 (validate) — now as a distributable unit |
| BR-11.2 (env decoupling) | BR-2, BR-3, NFR-6 (central config) — removes host coupling |
| BR-11.3 (scheduler) | BR-4.4, BR-4.5, BR-4.6, NFR-9 (resumability) |
| BR-11.4 (multi-user) | BR-9 (auth), BR-9.5 (roles), NFR-11 (credential hygiene) |
| BR-11.5 (MCP for any agent) | BR-10 (MCP), BR-10.3 (pipeline parity), BR-4.7 (MCP as trigger) |
| BR-11.6 (community) | New — no existing BR covers packaging/licensing/docs for release |

The existing BR-1..BR-10 and NFR-1..NFR-11 continue to apply unchanged.
This document adds BR-11 (with sub-requirements) and NFR-12..NFR-17.
