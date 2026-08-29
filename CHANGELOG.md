# Changelog

All notable changes to `digital-twins` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

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
