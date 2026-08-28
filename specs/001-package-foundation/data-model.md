# Data Model: Portable Package Foundation

## Entities

### Configuration (layered, runtime-resolved)

The resolved four-layer merge (env → `kb.local.yml` → `kb.yml` → built-in defaults). Not persisted as an entity; re-resolved on every command.

- `state_dir`, `config_dir` — paths (env-overridable)
- `qdrant.url`, `qdrant.api_key?` — vector-store endpoint
- `neo4j.url`, `neo4j.user`, `neo4j.password` — graph endpoint
- `llm.endpoint`, `llm.model`, `llm.api_key?` — OpenAI-compatible endpoint
- `embedding.model` (pinned default), `embedding.dim` (derived from model), `embedding.device` (`auto|cpu|cuda`)
- `chunking.max_chars`, `chunking.overlap`
- `sources.<name>` — see Source

### Source

A named ingestion channel, built-in or user-defined.

- `name` — unique key
- `enabled` — bool; **false for every source on a fresh install** (BR-11.2.7)
- `type` (`builtin`) or `entrypoint` (`module:factory`) — for custom sources
- Capability declaration: `runtime` (agent runtime it reads, e.g. `hermes`/`pi`/`dsh`; null = host-neutral), `credential` (env-var name it requires; null = none), `prefix` (stamped onto `source_url`)
- Per-source knobs: `max_items` (per-run cap), `timeout_s`
- `extra` — source-specific settings (state-DB path, IMAP host, PG DSN, directory…)

### IngestItem

- `source_url` — `prefix + item key` (provenance-stable)
- `payload` (text), `content_hash`
- Tags: source name; run attribution

### Point (Qdrant)

- Deterministic ID: `f(prefix, item key, content hash)` — stable across all triggers
- Vector (embedding of the chunk)
- Payload: `source_url`, tags, `run_id`, `ingested_at`
- Relationship: 1:1 with a committed IngestItem (the dedup guarantee)

### HighWater

- Key: `(source, item_key)` → last committed content hash / cursor
- Skips already-ingested items on re-run (resumability, NFR-9)

### AuditRun

- `run_id` (uuid), `started_at`, `completed_at`, `status` (`ok|partial|failed`)
- `trigger` — `manual` (this slice); reserved: `schedule|mcp|api|ui`
- `scheduled_by` — user or `system` (defaults to `system` in this slice)
- `per_source_counts` — JSON `{source: {new, skipped, failed}}`

### Account

Minimal in this slice (multi-user is a later feature): `id`, `email`, `role` (default `reader`), password hash. Created empty by `init`; first-account-becomes-admin applies when the accounts feature lands.

### HealthReport

- Per endpoint: `ok`, `detail`, `remediation`
- Plus: embedding dimension check result; per-enabled-source prerequisite results

## Validation rules (from requirements)

- All sources disabled on fresh install (FR-006)
- Enabling a source with a missing declared prerequisite → fail-fast error naming it (FR-008), checked at enable-time and run-time
- `embedding.dim` vs collection vector-size mismatch → hard error with remediation (FR-010)
- No undocumented knob: example files must match the `knobs.py` registry (FR-009)
- An audit row is written for every run, regardless of outcome (Constitution V)
