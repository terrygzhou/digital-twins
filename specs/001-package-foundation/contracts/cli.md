# CLI Contract

Entry point: `digital-twins` (console script) and `python -m digital_twins`.

| Command | Purpose | Exit codes |
|---|---|---|
| `digital-twins --version` | print machine-readable version | 0 |
| `digital-twins --version-json` | print version as JSON (`{"name", "version"}`) | 0 |
| `digital-twins init` | first-run setup | 0 ok; 1 failure |
| `digital-twins run` | one-shot ingestion | 0 ok; 1 config/validate failure; 2 fail-fast prerequisite |
| `digital-twins validate` | health check | 0 healthy; 1 unhealthy |

## `digital-twins init`

- Prompts for (or reads from config/env) the Qdrant, Neo4j, and LLM endpoints; `--yes` accepts current/defaults without prompting.
- Creates: config dir with a starter `kb.local.yml` (every source `enabled: false`), state dir, `state.db` (migrations run).
- Runs `validate` and prints the health report.
- Idempotent: re-running is safe — existing values are kept, only missing pieces prompted; an interrupted init resumes cleanly.

## `digital-twins run [--source NAME...] [--max-items N] [--dry-run]`

- Resolves config, runs migrations, then:
  1. **Fail-fast**: every enabled source is prerequisite-checked before ingestion; a missing prerequisite → exit 2, error names the missing prerequisite (runtime/credential/path), nothing ingested.
  2. Ingests the enabled sources (or only the `--source`-named ones) in config order.
  3. Upserts points (deterministic IDs), updates high-water marks, writes one audit row.
- Prints per-source counts + `run_id`.
- `--dry-run` performs all checks and reads, writes nothing.

## `digital-twins validate [--verbose]`

- Checks: Qdrant reachable + collection exists/creatable + vector-dimension match; Neo4j reachable + auth OK; LLM endpoint reachable; prerequisites of every enabled source.
- Prints a health table (endpoint, ok/fail, detail, remediation). Exits 0 only if all configured checks pass.

## Error-message shape (fail-fast)

`digital-twins: source 'hermes' cannot run: missing prerequisite — readable session store at <configured path> (set KB_HERMES__EXTRA__STATE_DB or sources.hermes.extra.state_db in kb.local.yml)`. Always names: the source, the missing thing, and where to set it.
