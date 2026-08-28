# Implementation Plan: Portable Package Foundation

**Branch**: `001-package-foundation` (no git repo yet — BRANCH empty; init one before committing) | **Date**: 2026-08-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-package-foundation/spec.md`

## Summary

Turns the host-wired Daily KB Session Ingest pipeline into an installable Python package `digital-twins`: four-layer config with all sources disabled by default, first-run `init`, endpoint + dimension health `validate`, named sources with capability declarations and fail-fast enablement (six built-ins + user-defined custom sources), and a one-shot `run` performing deterministic-ID ingestion into user-supplied Qdrant/Neo4j with dedup, high-water marks, and per-run audit records. No host paths, host usernames, or host runtime pins anywhere in shipped code, config, or docs.

Out of scope for this slice (later features): `serve` scheduler + cadence presets, multi-user sign-up/roles/tokens, MCP scheduler tools, web app surface, Docker bundle, PyPI publication mechanics.

## Technical Context

**Language/Version**: Python, `requires-python >= 3.11` (declared in manifest; BR-11.2.6 — host's 3.12 satisfies it, no hidden pin)

**Primary Dependencies**: `click` (CLI), `PyYAML` (config layer), `python-dotenv` (env layer), `qdrant-client`, `neo4j` (official driver), `sentence-transformers` + `torch` (embedding; BGE-small-en-v1.5 pinned)

**Storage**: local SQLite (stdlib `sqlite3`, WAL) in the state dir — accounts, high-water marks, audit runs; user-supplied Qdrant (vectors) + Neo4j (graph)

**Testing**: pytest; Qdrant in-memory/local mode; Neo4j/LLM stubbed in unit tests; live-service checks opt-in via env

**Target Platform**: any host with the declared Python range + network access

**Project Type**: Python package (library + CLI), single project

**Performance Goals**: CPU embedding of a 24h session window (thousands of points) completes in sane wall time; no concurrency requirement in v1

**Constraints**: no host-specific values in shipped code/config/docs (NFR-13, enforced by an automated scan); embedding model pinned, dimension mismatch = hard error (NFR-2); all sources disabled on fresh install (BR-11.2.7)

**Scale/Scope**: single host, one operator in this slice, six built-in + arbitrary user sources, thousands of points/day

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How it is met |
|---|---|---|
| I. Portability & Environment Neutrality | PASS | Every host value flows through the config layer; `tests/integration/test_portability.py` scans shipped code/config/docs for host paths, usernames, install locations (SC-003) |
| II. Deterministic, Idempotent Ingestion | PASS | Deterministic point IDs + Qdrant upsert + high-water marks; `tests/integration/test_idempotency.py` re-runs ingestion and asserts exactly one point (the one-record-not-N invariant) |
| III. Test-First (NON-NEGOTIABLE) | PASS | Task order writes invariant tests (idempotency, fail-fast, portability scan, knob-doc sync) before the feature code they govern |
| IV. Config-First, Fail-Fast | PASS | Typed config schema is the single source of truth; shipped example files are diffed against the schema's knob list (SC-002); enable-time and run-time prerequisite checks; errors name the missing prerequisite |
| V. Auditability & Observability | PASS | Every `run` writes an audit row: run_id, trigger (`manual` in this slice; `schedule/mcp/api/ui` reserved), scheduled_by, per-source counts, started_at/completed_at; queryable by user |
| VI. Upgrade Safety & Versioning | PASS | SQLite `user_version` + migration runner completing before any command; upgrade test asserts state + accounts + config survive |

**Result: PASS** — no violations; no Complexity Tracking required.

*Re-evaluated post-design (Phase 0/1): PASS — research and design surfaced no new violations (all research.md decisions are config-first and host-neutral).*

## Project Structure

### Documentation (this feature)

```text
specs/001-package-foundation/
├── spec.md            # feature spec (done)
├── plan.md            # this file
├── research.md        # Phase 0 output
├── data-model.md      # Phase 1 output
├── quickstart.md      # Phase 1 output
├── contracts/
│   ├── cli.md         # CLI command schemas
│   ├── config-schema.md # documented config schema (every knob)
│   └── source.md      # source adapter contract
└── tasks.md           # Phase 2 output (via /speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
digital-twins/
├── pyproject.toml            # deps, requires-python>=3.11, console-script entry, MIT license
├── .env.example              # every env knob documented (KB_* vars)
├── config.example.yml        # kb.yml committed-defaults example, every knob documented
├── README.md                 # quickstart + config reference + add-a-source how-to
├── CHANGELOG.md
├── LICENSE                   # MIT
├── digital_twins/
│   ├── __init__.py           # __version__ (single source)
│   ├── __main__.py           # python -m digital_twins
│   ├── cli.py                # init | run | validate | --version (click)
│   ├── config/
│   │   ├── loader.py         # env -> kb.local.yml -> kb.yml -> built-ins merge + precedence
│   │   ├── schema.py         # typed config model + validation
│   │   └── knobs.py          # machine-readable knob registry (schema <-> example sync guard)
│   ├── state/
│   │   ├── db.py             # sqlite connect (WAL), state-dir resolution
│   │   ├── migrations.py     # user_version runner (runs before any command)
│   │   └── models.py         # accounts, highwater, audit_runs
│   ├── sources/
│   │   ├── base.py           # Source contract + Capability + registry
│   │   ├── hermes.py         # hermes session-DB source
│   │   ├── pi.py             # pi sessions source
│   │   ├── dsh.py            # dsh sessions source
│   │   ├── paperclip.py      # paperclip PG chat source
│   │   ├── imap_mail.py      # shared IMAP source (yahoo + gmail instances)
│   │   ├── fs.py             # generic directory-of-files source (demo/test)
│   │   └── custom.py         # user-defined source loader (python entrypoint)
│   ├── ingest/
│   │   ├── ids.py            # deterministic point-ID scheme
│   │   ├── chunking.py       # config-driven chunk params
│   │   ├── embedding.py      # pinned BGE-small-en-v1.5, device knob, dim check
│   │   └── pipeline.py       # read -> chunk -> embed -> upsert Qdrant+Neo4j -> highwater -> audit
│   └── health.py             # validate: endpoints, dim, prerequisites
└── tests/
    ├── unit/                 # config precedence, knob-doc sync, ids, chunking, fail-fast, audit shape
    └── integration/          # in-memory Qdrant: idempotent re-run, dim mismatch, portability scan
```

**Structure Decision**: single project (default option). Domain SDKs (qdrant-client, neo4j, sentence-transformers) are the only heavy dependencies; CLI/config/state stay stdlib + click + PyYAML + dotenv. Baseline host scripts (`~/.hermes/skills/hermes/personal-kb/scripts/`) are reimplemented inside `digital_twins/sources/` + `ingest/` (FR-004) and never referenced at runtime.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

None — all gates PASS on first check; no justification needed.
