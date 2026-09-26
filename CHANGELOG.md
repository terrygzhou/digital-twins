# Changelog

All notable changes to `digital-twins` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

## [Unreleased]

### Changed
- `install.sh` / `install-local.sh` UX for non-technical users:
  - `--help` under `curl … | bash` (stdin-fed script) now prints a
    short one-liner summary instead of nothing — `usage()` falls back
    when `BASH_SOURCE` is empty.
  - `--quiet` removed from the pip install calls so download/progress
    lines are visible during the step that can take minutes.
  - When stdin is not a terminal and no cloud mode was given, the
    installer prints the two non-interactive paths (`--cloud-env` with
    the `KB_*` env vars, or `--no-setup` now + a later interactive
    `digital-twins setup`) before launching the wizard — a non-
    interactive failure is documented, not a surprise exit 6.
  - The closing banner is now a numbered next-steps block (activate
    venv → first ingest → web UI → admin-credentials path → uninstall
    pointer).
  - Exit-6 remediation names the exact re-run command.
- `README.md`: document `--cloud-env`, exit code 6, and the
  non-interactive stdin warning.

### Fixed
- **Local embedder is offline-safe.** `load_embedder()` now passes
  `local_files_only=True` to `SentenceTransformer`, so the pinned
  embedding model is read from the local HuggingFace cache
  (`~/.cache/huggingface`) only — the loader no longer probes
  HuggingFace on cold start, which previously failed with a
  DNS/egress error on air-gapped or no-HF hosts and aborted the
  embedding step. **Behaviour contract:** the model must now be
  pre-cached (e.g. `huggingface-cli download BAAI/bge-small-en-v1.5`,
  or one run with egress to populate the cache); a fresh host with an
  uncached model gets a fail-fast error pointing at the offline path
  and the `embedding.endpoint` (`KB_EMBEDDING__ENDPOINT`) alternative,
  instead of an implicit download. Regression guard:
  `tests/unit/test_embedding.py::test_load_embedder_forwards_model_and_device`
  now asserts the `local_files_only=True` flag is forwarded.

## [0.11.2] - 2026-09-26

### Added
- **Dashboard left-column overview panels** (`7100d1b`): the dashboard
  page now has a left side track with an account panel, an ingestion
  channels panel, a batch jobs panel, and a models-updating panel
  next to the main dashboard content.
- **Email channel settings surfaced** (`0085574`): the channel row
  on the dashboard now exposes the email account and IMAP host knobs
  so channel settings (email account, etc.) are visible without
  editing config files.

### Changed
- install.sh / install-local.sh: use `uv` (venv + pip) when the `uv`
  binary is already on PATH — faster installs, no dependency on the
  stdlib `venv` module; otherwise fall back to stdlib venv + the
  venv's own pip.  Neither installer ever installs `uv` for you.
  `FAKE_UV_PRESENT` flag file lets the mocked-exec tests steer the
  branch.  New tests: uv-present uses `uv venv` + `uv pip install`;
  uv-absent falls back to stdlib venv; venv-module-missing + uv
  present still succeeds.
- uninstall-local.sh: detect venv-based installs (stdlib or uv) via
  the venv's own pip / `uv pip show` and uninstall with the matching
  tool (`<venv>/bin/pip uninstall` or `uv pip uninstall --python …`);
  system pip remains the default for non-venv installs.

### Fixed
- **Dashboard two-track layout** (`e0364a0`): `main`'s `max-width`
  was clamping the new two-track grid; the override lets the
  left-column panels and main content lay out side by side.
- **setup wizard now re-prompts for Neo4j credentials** when a cloud
  Neo4j URL is given but `neo4j.user` / `neo4j.password` are left
  empty — the previous behaviour wrote an incomplete `kb.local.yml`
  and the health check reported `neo4j FAIL unconfigured`.  Each
  field is re-prompted up to 3 times with a "[empty for
  auth-disabled Neo4j]" hint; pressing Enter through all prompts
  still works for auth-disabled instances.  New regression tests:
  `test_cloud_neo4j_credentials_reprompt_when_empty`,
  `test_cloud_neo4j_auth_disabled_stays_clean`,
  `test_cloud_whitespace_in_endpoints_is_stripped`.
- **setup wizard strips whitespace from every endpoint** the user
  types (qdrant / neo4j / llm / embedding).  A pasted URL with a
  trailing space previously failed the LLM health check with an
  opaque `InvalidURL: control characters` error; the value is now
  `.strip()`-ed before being written to `kb.local.yml`.

## [0.11.1] - 2026-09-22

### Changed

- **`setup` wizard now names the real paths** it touches, so a host that
  re-runs the installer (idempotent branches) can find its files:
  - the "kb.local.yml already has valid endpoints" branch now prints the
    config-dir path (e.g. `~/.config/digital-twins/kb.local.yml`), not just
    the bare filename — the config dir and the state dir are *separate* by
    default.
  - the "admin already exists" re-run branch now points at
    `admin-credentials.txt` and notes it is written once and never re-printed.
- **`README.md` gains a "Where your files live" callout** distinguishing the
  config dir (`~/.config/digital-twins/`) from the state dir
  (`~/.digital-twins/`) and naming the `KB_CONFIG_DIR` / `KB_STATE_DIR`
  overrides.

### Docs

- **The README Uninstall section now states where `uninstall-local.sh`
  lives** (the repo's `scripts/` dir, *not* the `curl … | bash install.sh`
  one-liner) and gives the no-checkout fetch path plus the bare `rm -rf`
  equivalent of `--remove-data` (with a `state.db` backup first).

### Fixed

- **`scripts/install.sh` detects a stale CLI and force-reinstalls.** A re-run
  on a host whose venv already held an older `digital-twins-kb` could leave
  `pip install --upgrade` a silent no-op, so the very next
  `$venv_bin setup` failed with "No such command 'setup'". The installer now
  reads the installed version, probes `--help` for the `setup` subcommand,
  and issues `pip install --upgrade --force-reinstall` when it is missing.
  Pure-bash string ops (no `head`/`grep`) so the mocked-exec tests with an
  empty `PATH` exercise every branch.

## [0.11.0] - 2026-11-14

### Changed

- **PyPI distribution name:** the PyPI distribution publishes as
  `digital-twins-kb` (the bare `digital-twins` name is blocked by PyPI's
  name-similarity check, which treats `-`/`_`/`.` as equivalent to the
  already-registered `digital-twins-kb`). The user-facing layers are
  unchanged:
  - PyPI distribution: `digital-twins-kb` (the only layer that carries the
    `-kb` suffix — it is what you pass to `pip install`)
  - CLI on PATH: `digital-twins` (unchanged)
  - Python import: `digital_twins` (unchanged)
- The top-level `digital-twins-kb/` **transition metapackage** dir from the
  0.11.0-dev plan is retired: with the main distribution now publishing as
  `digital-twins-kb`, a separate zero-code shim depending on the bare name
  is redundant. No metapackage is built or uploaded with this release; the
  release runbook's metapackage step is removed. The `digital-twins-kb`
  artifact that exists on PyPI from 0.9.0 is superseded by this release.

### Added

- **One-shot installers**: `scripts/install-local.sh` (run from a checkout:
  finds Python ≥3.11, creates an isolated venv, pip-installs, runs the
  `setup` wizard) and `scripts/install.sh` — the
  `curl -fsSL …/scripts/install.sh | bash` remote entry point with a PEP 668
  venv-module check. Both are host-neutral (NFR-13), idempotent, and
  `run_cmd()`-interceptable; a `--dist PATH|NAME` flag installs a local
  wheel/dir instead of the PyPI dist.

## [0.10.0] - 2026-09-21

### Added

- **Install-path hardening**: compose file shipped in the wheel so `setup`
  works from a plain `pip install` (no git checkout required);
  `llm`/`embedding-model` ports published for host-side CLI access

### Fixed

- **embedding-model compose service (A5)**: the reference service now runs a
  deterministic 384-dim embedding stand-in with a working healthcheck;
  digital-twins image tag bumped 0.5.0 → 0.10.0

## [0.9.0] - 2026-08-31

### Added

- **Web admin Services panel** (009, US1/FR-001/002): admin-only card on the
  KB view showing the four hard services (qdrant / neo4j / llm / embedding)
  from the 008 masked view — effective URL, `*_set` credential badges,
  env-override badges, and per-row **Save** that persists display-field
  → knob mappings through the existing 008
  `POST /api/config/services` (atomic `kb.local.yml` merge; re-render from
  the post-write view).
- **Service probe route + Test / Test-all** (009, US2/FR-003, SC-002): new
  admin-only `POST /api/config/services/probe` runs the requested
  `digital_twins.health` checks in parallel under a shared 4.5 s wall-clock
  deadline, so the full four-service probe always answers inside the 5.0 s
  budget; timed-out entries return `unreachable` with the timeout line and a
  remediation naming the url knob + env form. Per-row **Test** and
  **Test all** buttons show a status pill + remediation line per service and
  suppress duplicate probes in flight.
- **Secret hygiene for the probe** (009, FR-006, SC-003): credentials are
  never echoed in the probe response or in `digital_twins` log records
  (existing hygiene contract extended to the new route; pinned by
  `test_secret_hygiene.py`).
- **Non-admins see nothing** (009, US3, SC-004): the panel is `hidden` in
  the markup and un-hidden only behind the `me.role === "admin"` check;
  non-admin probe requests get the pinned 403 `permission_denied` shape.
- **Web admin credential fields** (011, FR-001..FR-003, SC-001/SC-002):
  the 009 Services panel now carries write-only credential inputs —
  `qdrant.api_key`, `neo4j.user` (plain text, not a secret),
  `neo4j.password`, `llm.api_key`, `embedding.api_key` — always blank on
  render, trimmed and omitted when blank on save, persisted through the
  existing 008 `POST /api/config/services`. The `GET` response shape is
  byte-identical to the 008/009 contract (values never echoed back;
  `*_set` flags only) and `web/app.py` is untouched — the slice is
  presentation-layer over the 008 config API.

### Fixed

- **`POST /api/kb/search` 500 on any query** (010, hotfix): the web
  handler now embeds the query (the 007 MCP counterpart always did) and
  iterates the real qdrant-client `QueryResponse` shape (`.points`), so
  search answers 200 with results instead of 500ing on `AttributeError`;
  an embedder failure returns the pinned 503
  `embedding_unavailable` shape. The MCP `_kb_search_body` direct
  iteration of the same `QueryResponse` is fixed too.

### Notes

- No new config knobs and no new dependencies — the 009/011 UI is a
  presentation layer over the 008 config API + `digital_twins.health`
  checks. First PyPI upload of the 0.9.0 line: 0.8.0 was the only
  previously published release.

## [0.8.0] - 2026-08-31

### Added

- **Fail-fast service gate** (008, FR-001/002, SC-001): every trigger path
  (scheduler, `run --once`, MCP, web UI, `serve` at startup) refuses to run
  when a hard dependency is unconfigured / unreachable / auth-failed — 0
  silent no-op runs. `validate`/`health` now render a 4-service status table
  (qdrant / neo4j / llm / embedding) with per-service `status`
  (`ok | unconfigured | unreachable | auth-failed`) and remediation naming the
  exact knob + env form.
- **External hosting mode** (008, FR-003/004, FR-010, SC-002): qdrant / neo4j
  / llm endpoints and credentials configure via env/`.env`/`kb.local.yml`
  (or the web admin API) and reach the clients on both validate and ingestion
  paths. New knobs `embedding.endpoint` + `embedding.api_key` for an
  OpenAI-compatible external embedder (`/v1/embeddings` via stdlib urllib,
  retry-once on 5xx, strict dimension guard); in-process
  BAAI/bge-small-en-v1.5 remains the default.
- **Web admin API** `GET`/`POST /api/config/services`: admin-gated masked
  view (credentials as `api_key_set` booleans + `env_overrides`), partial
  updates persisted to `kb.local.yml` via atomic `merge_write` (401/403/404/422/409
  error shapes; `chunking` accepted as the one non-service target).
- **`scripts/bootstrap-local.sh`** (008, FR-005..008, SC-003/004): one-shot
  local setup on a clean Docker host — docker/compose detection,
  port-conflict probe (6333/7474/7687/8000/8080), GPU probe with
  `LLM_SERVICE` override, pin-hash + image-presence check, pull/build only
  when needed, health poll loop (`BOOTSTRAP_TIMEOUT_S`), `kb.local.yml`
  written only when absent. Exit codes 0/1/2/3/4; `--status`/`--help`.
  On no-GPU hosts the bundled llm service is skipped with a warning and
  external-LLM remediation (BR-12.3.4).
- **`digital_twins/config/local_io.py`**: machine-local config layer —
  `local_config_path()` + `merge_write` (round-trip, preserves unrelated
  keys, atomic, refuses unparseable/out-of-dir targets).
- 111 new tests (831 → 942).

### Changed

- `run_pipeline` preflights before any write: on dependency failure raises
  `ServiceDependencyError(service, status, remediation)` — no audit row, no
  partial writes on the `run_pipeline` / CLI / MCP / web paths. The
  scheduler keeps its pre-existing R-07 backstop (`failed` row, never
  silent); the no-row rule and this ruling are recorded in
  `specs/008-service-hosting/plan.md`.
- `validate`/`health` CLI output: 4-service table with `status` +
  remediation (`contracts/cli.md` updated).
- Portability guard now scans `scripts/` + python-interpreter-pin patterns;
  `test_knob_docs.py` covers the two new embedding knobs.

### Notes

- **Distribution name (superseded in 0.11.0):** at this release the PyPI package was `digital-twins-kb` — the bare name `digital-twins` was believed to be blocked by PyPI's name-similarity policy (existing projects `digital-twin` / `digitaltwins` / `digital.twin`). The import package (`digital_twins`) and CLI command (`digital-twins`) were unchanged. **0.11.0 renamed the dist to `digital-twins`** after the bare name was confirmed free (live PyPI 404 check); a `digital-twins-kb` transition metapackage was shipped so the old pip command kept working.
- Quickstart §5 live-Docker proofs (clean GPU host, no-GPU host) remain
  manual follow-ups per A8 (005 c4 pattern); the mocked/static surface is
  fully green.

## [0.7.0] - 2026-08-31

### Added

- **Real MCP KB tools** (007 slice, BR-11.5.2 / BR-11.5.3 / BR-10) — the four
  BR-10 stub tools from 004 are now real implementations:
  - **`kb_search`** — owner-scoped Qdrant vector search on the `personal_kb`
    collection. Query is embedded via the config-pinned embedding model
    (`embedding.model` / `embedding.device`); results are filtered by the
    caller's `owner_tag` (`accounts.owner_tag_for`). Returns top-N
    (default 5, max 100) with `score`, `source_url`, `text`, `source`,
    `chunk_index`. Errors: `bad_request` (blank/missing query),
    `qdrant_unavailable` (Qdrant not reachable), `embedding_unavailable`
    (embedding model load failed), `config_not_loaded`.
  - **`kb_chat`** — the chat **surface** (007 R8 / 006 C-1 parity): reads
    `llm.endpoint` / `llm.model` from the config layer (decision-ready for a
    follow-up slice) and returns `not_implemented` with a remediation hint
    until generation is implemented. No LLM call, no embedding call, no
    Qdrant call, no network call.
  - **`kb_ingest`** — triggers a KB ingestion run via
    `ingest.pipeline.run_pipeline` with `trigger="mcp"`,
    `scheduled_by=<caller-email>`, `owner=<caller-email>` (the same code
    path as schedule / `run --once` / web UI — NFR-1/NFR-14 one-record
    holds). Capability-gated on `trigger_run` (reader → `permission_denied`,
    no audit row). Source validation mirrors 006 web
    (`unknown source` / `source is not enabled` / `no sources enabled`).
    The audit row carries `trigger="mcp"` + `agent_kind` (stamped via the
    004 `_stamp_agent_kind` post-call pattern; BR-11.5.3).
  - **`kb_health`** — wraps `health.run_health_checks` and returns
    per-endpoint results (`endpoint`, `ok`, `detail`, `remediation`).
- **Full `inputSchema`s** for all four tools in the MCP registry
  (replacing 004's minimal `_stub_schema()`). Descriptions now describe
  real behavior + error codes.
- **`MCPContext.config`** — the MCP context gains a `config: Any = None`
  field (last, so the 004 4-arg positional call shape is preserved). Both
  transports (stdio + http) populate it from the config layer
  (`digital_twins.config.loader.load()`); `cli.serve_mcp` threads its
  already-loaded `cfg` into both. KB tool bodies fail closed with
  `config_not_loaded` when it is `None`.

### Changed

- Replaced the four BR-10 stub tool bodies from 004
  (`not_implemented_yet` → real behavior). The `_stub_schema()` helper in
  `digital_twins/mcp/registry.py` is deleted (no remaining callers).
- `tests/unit/test_mcp_stubs.py` deleted (the four tools are no longer
  stubs; their real-body tests supersede it).
- `tests/integration/test_mcp_integration.py::test_sc001_fresh_client_gets_full_tool_list`
  updated: the 4 KB tools no longer assert `not_implemented_yet`; the
  assertion now checks that their observed error codes stay within the 007
  error-shape set.

## [0.6.0] - 2026-08-30

### Added

- **Web app first-class surface** (006 slice, BR-11.1.8) — the package now
  ships a standalone web UI on a dedicated port (8767):
  - **WebApp scaffold** (`digital_twins/web/app.py`): stdlib
    `ThreadingHTTPServer` + `BaseHTTPRequestHandler` (no new web framework);
    bearer-token gate via `digital_twins.auth` (`Authorization: Bearer <token>`
    or `?token=` fallback, fail-closed 401); path-traversal-safe static routes
    (`/` → `index.html`, `/static/*` → `web/static/`); `/api/*` dispatch.
  - **Auth surface**: `/api/auth/signup` (first account → admin, else reader),
    `/api/auth/signin`, `/api/auth/signout` (revoke session), `GET /api/me`
    (email, role, point_count; point_count degrades to 0 when Qdrant
    unreachable).
  - **KB read**: `GET /api/kb/points` (count, owner_count, source_count,
    sample — owner-scoped via `owner_tag`) and `POST /api/kb/search`
    (owner-scoped vector search via Qdrant + `digital_twins.ingest.embedding`;
    502/503 with remediation hint when Qdrant is down).
  - **Ingestion trigger**: `POST /api/ingest/run` — delegates to
    `digital_twins.ingest.pipeline.run_pipeline` with `trigger='web'` +
    `scheduled_by=<caller email>` + `owner=<caller owner_tag>` (one-record-not-N
    invariant, NFR-1/NFR-14). Reader role → 403 `permission_denied` before any
    pipeline work. Disabled/unknown source → 400 with contract-exact error.
  - **Audit**: `GET /api/audit/recent` — per-user scoping (non-admin sees only
    their own `scheduled_by` rows; admin sees all). `per_source_counts`
    returned as a decoded dict.
  - **Chat surface**: `POST /api/kb/chat` — 501 `not_implemented` with
    remediation hint when `llm.endpoint` is unset (mirrors 004's BR-10 stub
    pattern; generation is a follow-up slice).
  - **CLI**: `digital-twins web` click subcommand — binds to
    `web.bind`/`web.port` (default 127.0.0.1:8767), prints the listening URL,
    serves the UI + API. No scheduler lifecycle (separate surface from
    `serve`).
  - **Static UI** (`digital_twins/web/static/`): single-page `index.html`
    (sign-in, sign-up, KB panel with point count / search / trigger-ingestion
    / last-audit-row) + `style.css`. Plain HTML, no build step, no SPA
    framework. Plain `fetch()` to `/api/*` + vanilla DOM. Host-neutral
    (relative `/api/*` paths only, NFR-13).
  - **Config knobs** (Constitution IV lock-step): `web.bind` (default
    127.0.0.1), `web.port` (default 8767), `web.base_url` (default empty).
    Four-surface lock-step: `knobs.py` registry, `config.example.yml`,
    `.env.example`, `docs/configuration.md`.
  - **Portability guard extended**: `test_portability.py` SHIPPED +
    SHIPPED_NON_PY now covers `digital_twins/web/` Python + static assets +
    `specs/006-web-app/quickstart.md` (NFR-13).
  - **Quickstart**: `specs/006-web-app/quickstart.md` — host-neutral
    install → serve → sign-in → query → trigger ingestion flow.

## [0.5.0] - 2026-08-30

### Added

- **Community/packaging** (005 slice) — the package is now ready for
  community use:
  - **Config reference** (`docs/configuration.md`): single authoritative
    human-readable config reference covering 100% of the `KNOBS` registry
    (46 keys) in six sections, with precedence + env-var mapping +
    deprecation mechanism. Guarded by
    `TestKnobsDocumentedInConfigurationDoc` (new class in
    `tests/unit/test_knob_docs.py`).
  - **README rewrite** (`README.md`): 9-section host-neutral quick start
    (install → init → validate → fs first run → roles → MCP → scheduling →
    community → license → development).
  - **SemVer policy** (`docs/semver-policy.md`): concise versioning policy —
    what counts as major/minor/patch, the deprecation mechanism, and the
    in-repo tracker cross-reference.
  - **In-repo GitHub issue tracker** (`.github/ISSUE_TEMPLATE/`): four
    forms — `config.yml` (index), `bug_report.yml`, `feature_request.yml`,
    `config-breaking-change.yml` (with required `version_impact` +
    `migration_note` fields). No live GitHub remote; the forms are the
    machine-readable entry point.
  - **Docker/Compose reference deployment** (`docker-compose.yml` +
    `Dockerfile`): 5-service bundle (qdrant, neo4j, digital-twins, llm,
    embedding-model), all images pinned, named volumes, healthchecks,
    `depends_on: service_healthy`. SGLang + BGE-small-en-v1.5 bundled by
    default (D-1). Guarded by `tests/integration/test_docker_compose.py`.
  - **Release runbook** (`docs/release-runbook.md`): repeatable release
    procedure (pre-release → build → automated check → TestPyPI manual →
    PyPI manual → tag → record image tags). Guarded by
    `tests/integration/test_pypi_build.py`.
  - **Agent guides** (`docs/references/agent-guides.md`): canonical MCP
    agent onboarding page — the six 004 scheduler tools with args, role
    requirements, and error codes; the four BR-10 stubs; transport
    (stdio + HTTP/SSE).
  - **Deprecation warning mechanism**
    (`digital_twins/config/deprecation.py`): one-run `DeprecationWarning`
    naming the replacement; wired into the config loader via
    `resolve(dotted, value)`. No shipped knob deprecated at 0.5.0.
    Guarded by `tests/unit/test_deprecation_warning.py`.

## [0.4.0] - 2026-08-30

### Added

- **MCP scheduler tools** (004 slice) — a full MCP server with six scheduler
  tools (`kb_schedule_list/create/update/delete/run`, `kb_run_history`) plus
  four BR-10 stubs (`kb_search/chat/ingest/health` → `not_implemented_yet`),
  exposing the 001–003 scheduler surface over stdio and HTTP/SSE so any
  MCP-capable agent can manage schedules and trigger runs (BR-11.5):
  - **MCP server from scratch** (R1): tool registry, stdio + HTTP/SSE
    transports, BR-10.5 token auth reusing 003's
    `verify_personal_token`/`verify_session`/`DT_SERVICE_TOKEN`. The `mcp`
    SDK is an optional extra (`pip install "digital-twins[mcp]"`); the
    server speaks raw JSON.
  - **Role-gated tools** (R4, 003 R3): create/update/delete →
    `schedule_crud`; run → `trigger_run`; history-own →
    `view_own_history`; history-all → `view_all_history`; list-own →
    `query_status`. A `reader` is denied every mutating tool with
    `code=permission_denied`.
  - **Caller-scoped default** (R5/Q9): tools operate on the caller's own
    schedules/history by default; admin sees all. `schedule.acl` (already in
    the 002 schema, default `'owner'`) is accepted by create/update;
    granular ACL enforcement is a documented follow-up.
  - **Pipeline parity** (one-record-not-N, NFR-1/NFR-14): `kb_schedule_run`
    calls the same 001 `run_pipeline` with `trigger="mcp"`,
    `scheduled_by=caller`, owner-tagged points — the same content ingested
    via MCP + CLI + web yields one point, not three.
  - **Audit parity** (BR-11.5.3): every `kb_schedule_run` writes one
    `audit_runs` row with `trigger='mcp'` + `agent_kind` (client-declared or
    `'unknown'`). Admin cross-user `kb_run_history` writes an access-log row
    (R8); schedule CRUD writes no `audit_runs` row (R11 — structured logging
    instead). `kb_run_history` excludes `mcp_history_query` rows from the
    runs view (R13).
  - **New config knobs** (R12): `mcp.port` (default `8770`),
    `mcp.service_account_email` (default `"system"`). Documented in
    `config.example.yml` + `.env.example` + `KNOBS` registry.
  - **Entry points**: `digital-twins serve-mcp --transport stdio|http
    [--port N]` + `python -m digital_twins.mcp`.

## [0.3.0] - 2026-08-29

### Added

- **Multi-user surface** (003 slice) — three legal roles, personal tokens,
  per-user config, and owner-tagged points, so the same content yields one
  point whether ingested by admin, scheduler, or reader (NFR-1, NFR-14):
  - **Three roles** (Q3, locked): `admin` (every capability, including
    cross-user account management and all-user run-history viewing),
    `scheduler` (manage schedules + trigger runs + own run history; cannot
    reconfigure endpoints, manage accounts, or edit global config; can
    manage its own personal tokens and config), and `reader` (pure query;
    mutating routes denied with 403). Role is a data value on
    `accounts.role`, not a schema enum (v3 migration is additive-only).
  - **Personal tokens** (C-1): separate `personal_tokens` table; a user can
    hold multiple tokens, each independently revocable. Tokens are stored as
    pbkdf2 hashes (`pbkdf2$salt_hex$hash_hex`), never plaintext. The CLI
    reads `DT_PERSONAL_TOKEN` to authenticate a command with a token instead
    of the password (`DT_USER_PASSWORD`). A personal token and a password are
    alternatives; the CLI uses the token when set, else the password.
  - **Per-user config** (R5/SC-003): `user_config` table stores per-user
    config overrides. The config loader merges user overrides on top of the
    global config layer (env → kb.local.yml → kb.yml → user overrides →
    built-in defaults). `digital-twins config set|list|unset` manages
    per-user overrides.
  - **Owner-tagged points** (R6/SC-005): `run_pipeline` accepts an `owner=`
    kwarg that is stored as a payload field on the point, not a dedup key.
    The same content ingested by two different owners still yields one
    point (NFR-1 one-record dedup invariant preserved).
  - **Owner-filtered run history** (T019): `list_runs` filters run history
    by owner; non-admin users see only their own runs.
  - **Web UI signup/signin** (T014): `/signup` and `/signin` endpoints on
    the web server, with session tokens (pbkdf2-hashed, stored in `sessions`
    table).
  - **Status endpoint auth gate** (C-5/R8): `serve` now accepts an
    `auth_checker` that gates `/status` with the BR-10 shared service token
    (`DT_SERVICE_TOKEN` env var).

- `digital_twins/state/models.py`: v3 migration (additive-only) adding
  `personal_tokens`, `user_config`, and `sessions` tables.
- `digital_twins/state/user_config.py`: per-user config merge logic.
- `docs/multi-user.md`: operator-facing multi-user guide (role capability
  matrix, credential env vars, owner-filter queries, first-admin / sign-up
  flow, per-user config merge precedence). Now covered by the portability
  guard (SC-006).
- `tests/integration/test_serve_once_multi_user.py`: multi-user serve tick
  integration tests.
- `tests/integration/test_owner_isolation.py`: SC-005 owner-isolation +
  NFR-1 dedup invariant tests.
- `tests/integration/test_pipeline_owner.py`: R6 owner-kwarg dedup invariant
  tests.

### Changed

- `state/migrations.py`: `SCHEMA_VERSION` bumped 2 → 3 (v3 migration is
  additive-only; no data migration required).
- `cli.py`: new `account` command group (list, set-role, delete, whoami),
  new `token` command group (create, list, revoke), new `config` command
  group (set, list, unset). `run --once --as` and `schedule` commands now
  perform post-auth role checks. All mutating CLI commands accept both
  `DT_USER_PASSWORD` and `DT_PERSONAL_TOKEN` for authentication.
- `cli.py`: `list_runs` now filters by owner (non-admin users see only
  their own runs).
- `scheduler/loop.py`: `serve_once_tick` now accepts an `owner` parameter
  and passes it through to `run_pipeline`.
- `web/server.py`: new `/signup` and `/signin` endpoints; session token
  management.
- `test_knob_docs.py` guard extended with an explicit allow-list for
  multi-user auth-only env vars (`DT_PERSONAL_TOKEN`, `DT_SERVICE_TOKEN`)
  so the knob-doc sync test does not flag them as missing knobs.

### Test suite

- Suite grows 289 (v0.2.0) → 536 (v0.3.0): multi-user serve tick
  integration tests, SC-005 owner-isolation + NFR-1 dedup invariant tests,
  R6 owner-kwarg dedup invariant tests, v2→v3 upgrade preservation tests,
  per-user config merge tests, personal token lifecycle tests, role
  capability matrix tests, and the quickstart scenario e2e.

## [0.2.0] - 2026-09-01

### Added

- **Scheduled runs** (002 slice) — a first-class scheduling layer over the
  001 ingestion pipeline, so the same content yields one point whether
  ingested on a schedule, via `run --once`, or manually (NFR-1, NFR-14):
  - `digital-twins serve` — a daemonized scheduler service that ticks on an
    interval, claims due schedules, advances them, and runs the ingestion
    pipeline for each. Runs a single `ThreadingHTTPServer` status endpoint
    (`GET /status`) on `scheduler.status_port` (default `8765`; set the port
    to `0` to disable the socket entirely).
  - `digital-twins run --once` — one-shot host-cron run: `trigger='manual'`,
    no schedule advance, no pidfile. Preserves 001's fail-fast + audit-row
    semantics.
  - `digital-twins schedule add|list|remove` — manage schedules in the
    `schedules` table (v2 migration). `--as` on `schedule add` is an owner
    label (ruling R-12), not an auth check.
  - **Preset cadences**: `daily`, `hourly`, `weekly`, `monthly`,
    `every-N-hours` — pure `expand_next()` expansion with month-length clamping
    (Jan 31 → Feb 28/29 → Mar 31, ruling R-10).
  - **Resumable runs**: high-water marks (001) are honored across
    kill/restart, so a 50-item source killed at item 19 resumes at item 20
    on the next fire rather than re-reading items 0–19.
  - **Per-run caps**: `sources.<name>.max_items` is read fresh on each tick,
    so lowering a cap takes effect on the next fire without a restart.
  - **Auth via `run --once --as USER`**: pbkdf2_hmac(sha256, 100k iters,
    16-byte salt) against the `accounts` store, password read from the
    `DT_USER_PASSWORD` env var (auth-only, not a config knob). Auth runs
    BEFORE any pipeline work; a failure exits 2 without touching state.

- `digital_twins/scheduler/` package: `presets.py` (cadence expansion),
  `schedules.py` (CRUD + due-claim), `loop.py` (`serve_once_tick` +
  `run_serve` pidfile/signal loop), `status.py` (`status_payload` +
  `ThreadingHTTPServer` handler).

- `docs/scheduling.md` — operator-facing scheduling guide (preset cadences,
  `serve`, `schedule` subcommands, `/status` shape, auth, resume + caps
  behavior). Now covered by the portability guard (SC-006).

### Changed

- `state/migrations.py`: new v2 migration adding the `schedules` table
  (`id`, `owner`, `source`, `preset`, `param`, `fire_time`, `created_at`,
  `next_fire_at`); `SCHEMA_VERSION` bumped 1 → 2. `state/models.py` gains
  `authenticate()` (pbkdf2 verify, timing-safe compare) and schedule
  upsert/advance helpers.
- `cli.py`: new `serve` and `schedule` command groups; `run` gains
  `--once` and `--as` flags.
- `test_knob_docs.py` guard extended with an explicit allow-list for
  auth-only env vars (`DT_USER_PASSWORD`, ruling R-06) so the knob-doc
  sync test does not flag it as a missing knob.

### Test suite

- Suite grows 165 (v0.1.1) → 289 (v0.2.0): preset expansion known-answers +
  property-style pins, schedule CRUD + clock-skew guard, serve tick +
  kill/restart resume, per-run fresh caps, source-pause mid-serve,
  `/status` payload shape + SC-004 wall-clock, port-0 disable, cross-trigger
  dedup (both orderings, consolidated into `tests/integration/
  test_serve_once_dedup.py`), and the quickstart scenario e2e.

## [0.1.1] - 2026-08-29

### Added

- **IMAP account-address knob** (closes the last SC-002 gap):
  `sources.yahoo.email` / `sources.gmail.email` are now first-class source
  knobs. The IMAP mail sources read the account address from config when set,
  otherwise fall back to the provider env var (`YMAIL_EMAIL` / `GMAIL_EMAIL`).
  The `init` starter config now documents both the `credential` env-var name
  and the `email` knob for yahoo/gmail.
- Two new unit tests lock in the config-wins / env-var-fallback behaviour
  (`tests/unit/test_imap_mail_source.py`).

### Changed

- `config.example.yml`, `.env.example`, `README.md` config reference:
  document the new `email` knob and the `YMAIL_EMAIL` / `GMAIL_EMAIL`
  account-address env vars.

## [0.1.0] - 2026-08-29

### Added

- **US1 — CLI skeleton, config layer, health checks**: `digital-twins` CLI
  entry point with `init`, `validate`, `run`, `--version` commands;
  layered config (env → kb.local.yml → kb.yml → built-in defaults) with
  `KB_*` env knobs; endpoint health checks (Qdrant, Neo4j, LLM) with
  actionable remediation hints; MIT license; `pyproject.toml` manifest.

- **US2 — Ingestion pipeline and sources**: deterministic point IDs
  (prefix|item_key|chunk|hash) for NFR-1 dedup; fail-fast prerequisite
  checks (exit 2, nothing ingested); high-water marks for incremental
  reads; one audit row per run; four built-in sources — `hermes`, `pi`,
  `dsh` (session logs), `fs` (filesystem); pinned embedding model
  (BAAI/bge-small-en-v1.5, 384-dim) with lazy sentence-transformers
  import; chunking with configurable max_chars/overlap.

- **US3 — Knob registry, config examples, load_debug**: `knobs.py`
  registry documenting every config knob with source, default, and
  env-var name; `load_debug()` API for layer-wins config inspection;
  shipped `config.example.yml` with all knobs and defaults; knob-doc
  sync test (SC-002) enforcing registry ↔ docs consistency.

- **US4 — Dimension guard and upgrade preservation**: `DimensionMismatchError`
  raised at `run` start when the Qdrant collection's vector dimension
  does not match the pinned model (S2-mismatch, NFR-2, exit 1);
  upgrade-preservation tests proving accounts, high-water marks, and
  audit runs survive version-bumped migrations (S7, NFR-15, SC-006,
  FR-012); config files on disk are untouched by migrations (FR-012).

- **US5 — Custom source loader**: `entrypoint` field in source config
  resolves a user-defined Python module implementing the Source
  contract; `CustomSourceError` for missing/invalid entrypoints;
  custom sources participate in fail-fast, dedup, and audit like
  built-ins.

- **Test suite**: 139 tests (unit + integration) covering config
  precedence, fail-fast, idempotency, dimension mismatch, upgrade
  preservation, custom sources, knob-doc sync, portability audit
  (no host paths in shipped code), and CLI behavior.
