# Research: Portable Package Foundation

Phase 0 output. All Technical-Context unknowns resolved; no open items.

## R1 — Python version range
- **Decision**: `requires-python = ">=3.11"` in `pyproject.toml`.
- **Rationale**: BR-11.2.6 requires a declared range, not a host pin. 3.11 matches the BRD's suggested floor; the host's 3.12 satisfies it.
- **Alternatives considered**: `>=3.12` (narrower, gains nothing for the host, shrinks the community install base); pinning exact 3.12 (violates BR-11.2.6 — host-specific assumption).

## R2 — CLI framework
- **Decision**: `click`, console-script entry point `digital-twins`.
- **Rationale**: the command set grows across later slices (`serve`, `run --as <user>`, schedule commands); click's subcommands/options pay off immediately and it is one small pure-python dependency.
- **Alternatives considered**: stdlib `argparse` (fine today; manual subcommand plumbing and option-parsing bloat in every later slice); `typer` (pulls pydantic — more than needed).

## R3 — Config layers and file placement
- **Decision**: four-layer precedence — process env (incl. `.env` via `python-dotenv`) → `kb.local.yml` → `kb.yml` → built-in defaults; deep-merge dicts, env wins at every level. Env vars use `KB_` prefix with `__` as the nesting separator (e.g. `KB_QDRANT__URL`). Config dir defaults to `~/.config/digital-twins/` (override `KB_CONFIG_DIR`); state dir defaults to `~/.digital-twins/` (override `KB_STATE_DIR`).
- **Rationale**: matches BR-11.2.1's named precedence exactly; XDG-style defaults are host-neutral and fully overridable; separating config from state means tool upgrades never touch user state.
- **Alternatives considered**: single TOML file (loses the env/local/committed split the BRD names); CWD-relative config only (breaks the run-from-anywhere CLI contract); hand-rolled `.env` parser (python-dotenv already handles quoting/comment edge cases for ~zero cost).

## R4 — Local state store
- **Decision**: one SQLite file `state.db` in the state dir (stdlib `sqlite3`, WAL mode) holding `accounts`, `highwater(source, item_key, last_key, updated_at)`, `audit_runs`.
- **Rationale**: stdlib (no new dependency), single file makes the upgrade-preservation test trivial (NFR-15), and schema versioning via `PRAGMA user_version` + a migration runner that completes before any command (Constitution VI).
- **Alternatives considered**: separate `users.db` + loose JSON state files (baseline shape; harder to keep atomic across upgrades); Qdrant-backed state (couples state lifetime to the vector service — wrong: state must survive with endpoints down).

## R5 — Deterministic point IDs and dedup (Constitution II)
- **Decision**: point ID = deterministic function of `(source prefix, source item key, content hash)`; points are upserted into Qdrant; high-water marks skip already-committed items on re-run.
- **Rationale**: NFR-1/NFR-14 — one record, not N, across trigger paths. The ID is stable across `run` invocations and (later) `serve`/MCP/UI triggers, so upsert is the only write path and duplicates are impossible by construction.
- **Alternatives considered**: per-run UUIDs + external dedup table (breaks cross-trigger idempotency); content-hash-only IDs (collides when two sources share content but have different provenance — the `source_url` prefix exists precisely to keep provenance, mirroring the baseline's `pi:`/`dsh:` prefixes).

## R6 — Embedding model and dimension guard
- **Decision**: pin the `sentence-transformers` model `BAAI/bge-small-en-v1.5` (384-dim) at a specific model version in the manifest; config knob `embedding.device: auto|cpu|cuda` (default `auto`); a dimension check compares the configured model's dim against the target collection's actual vector size at `validate` and before first write — mismatch is a hard error with a remediation message (re-embed, or point at a new collection).
- **Rationale**: BR-11.1.6/NFR-2 forbid silent vector-space drift; the host's `CUDA_VISIBLE_DEVICES=""` is a host pin, so it ships as a *config knob* (device) rather than a baked default (Constitution I).
- **Alternatives considered**: shipping `CUDA_VISIBLE_DEVICES=""` as the default (violates Constitution I / NFR-13); allowing dim mismatch with a warning (explicitly forbidden by NFR-2).

## R7 — Neo4j and LLM integration surface
- **Decision**: official `neo4j` driver; ingestion writes session/derived nodes + links alongside points; the LLM is consumed as an OpenAI-compatible HTTP endpoint (SGLang-compatible) — in this slice only `validate` touches it (connectivity + model reachable).
- **Rationale**: the thinnest surface that keeps later slices (chat, entity enrichment) on the same endpoint config; SGLang compatibility follows the BRD's bundled-LLM direction.
- **Alternatives considered**: Neo4j Aura assumptions (no — endpoints are user-supplied); LLM-based chunking now (deferred to a later slice; chunking is deterministic and config-driven in this slice).

## R8 — Custom (user-defined) sources
- **Decision**: `sources.<name>` config entries either name a built-in (`hermes`, `pi`, `dsh`, `paperclip`, `yahoo`, `gmail`, `fs`) or declare `entrypoint: "module:factory"` (a user-importable Python factory returning a `Source`); the capability (required runtime, credential env-var name, stamped prefix) is declared in the config entry. `fs` (directory of files) ships as a generic built-in usable for demos/tests.
- **Rationale**: FR-011 — new sources without a package update; because the capability declaration lives in config (not code), fail-fast checks work uniformly for built-ins and custom sources.
- **Alternatives considered**: plugin-directory scanning (more machinery, same capability); restricting custom sources to a fixed set of generic adapters (fails FR-011's arbitrary-channel requirement).

## R9 — Testing strategy
- **Decision**: pytest; in-memory/local Qdrant for pipeline tests; stubbed Neo4j + LLM transports for unit tests; live-service checks opt-in via env vars (not run in the default suite). Invariant tests (idempotency, portability scan, knob-doc sync, dim mismatch) land **before** the feature code they guard (Constitution III).
- **Rationale**: fast hermetic default suite + opt-in realism; the constitution's invariants get executable checks, not prose.
- **Alternatives considered**: testcontainers for Qdrant/Neo4j (heavier, needs Docker in CI — defer to a later slice's CI setup).

## R10 — Build/packaging backend
- **Decision**: `hatchling` build backend; package data includes the example config files; `__version__` single-sourced in `digital_twins/__init__.py`.
- **Rationale**: modern minimal backend, one-file pyproject; version available to both `digital-twins --version` and imports (FR-005).
- **Alternatives considered**: setuptools (fine, more boilerplate for this shape); poetry (adds lockfile + venv management the BRD does not ask for).

## R11 — Audit schema now, scheduler later
- **Decision**: `audit_runs` carries the BR-11.3.3 fields from day one: `run_id`, `trigger` (`manual` in this slice; `schedule|mcp|api|ui` reserved), `scheduled_by` (default `system` in this slice), per-source counts (JSON column), `started_at`/`completed_at`, status.
- **Rationale**: Constitution V requires per-run attribution; adding the columns later is a migration, adding the *behavior* now is a line.
- **Alternatives considered**: minimal audit table extended in the scheduler slice (double migration + audit-shape gap across triggers).

## R12 — Baseline absorption (FR-004)
- **Decision**: reimplement the seven host-script pipeline steps inside `digital_twins/sources/` + `digital_twins/ingest/`; the host scripts remain a *reference* during implementation only. `pi`/`hermes`/`dsh` source config entries default `enabled: false` and declare their prerequisite (a readable session store), so a community host without those runtimes fails fast cleanly instead of assuming them.
- **Rationale**: BR-11.1.4 self-containment + BR-11.2.7 all-disabled default; one codebase, zero runtime host files.
- **Alternatives considered**: shelling out to the host scripts (violates BR-11.1.4); copying the scripts verbatim into the package (preserves host-path assumptions — the anti-goal of this slice).
