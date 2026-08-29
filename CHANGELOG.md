# Changelog

All notable changes to `digital-twins` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

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
