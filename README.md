# digital-tokens

Environment-portable KB ingestion: layered config, fail-fast named sources, and
 deterministic dedup-safe ingestion into user-supplied Qdrant + Neo4j.

Built per `specs/001-package-foundation/` (see `AGENTS.md` for sources of truth).

## Install

```bash
python3 -m venv --system-site-packages .venv   # reuse heavy system deps (torch, etc.)
.venv/bin/pip install -e .
```

Requires Python ≥ 3.11. Heavy dependencies (torch, sentence-transformers,
qdrant-client, neo4j) are declared in the manifest; on hosts that already carry
them, `--system-site-packages` avoids re-downloading.

## Commands

| Command | Status |
|---|---|
| `digital-tokens --version` | ✅ |
| `digital-tokens init` | in progress (US1) |
| `digital-tokens validate` | in progress (US1) |
| `digital-tokens run` | in progress (US2) |

## Configuration

Two shipped example files document every knob: [`config.example.yml`](config.example.yml)
(committed defaults, `kb.yml`) and [`.env.example`](.env.example) (environment layer).
Precedence: **env (incl. `.env`) → `kb.local.yml` → `kb.yml` → built-in defaults**.
All sources are disabled by default.

Full config reference: lands with User Story 3 (`specs/001-package-foundation/tasks.md`, T030).
