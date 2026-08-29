# Changelog

All notable changes to `digital-twins` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

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
