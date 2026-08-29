# digital-twins

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
| `digital-twins --version` | ✅ |
| `digital-twins init` | in progress (US1) |
| `digital-twins validate` | in progress (US1) |
| `digital-twins run` | in progress (US2) |

## Configuration

Two shipped example files document every knob: [`config.example.yml`](config.example.yml)
(committed defaults, `kb.yml`) and [`.env.example`](.env.example) (environment layer).
Precedence: **env (incl. `.env`) → `kb.local.yml` → `kb.yml` → built-in defaults**.
All sources are disabled by default.

## Configuration Reference

Authoritative schema: [`specs/001-package-foundation/contracts/config-schema.md`](specs/001-package-foundation/contracts/config-schema.md).
Full documented surface: [`config.example.yml`](config.example.yml) and [`.env.example`](.env.example).

### Precedence Order

Four layers; highest wins:

1. **Environment variables** (incl. `.env`) — `KB_` prefix, `__` = nesting
2. **`kb.local.yml`** — machine-local overrides (untracked, never committed)
3. **`kb.yml`** — committed defaults
4. **Built-in defaults** — from the package

**Env mapping rule:** `KB_` prefix, `__` replaces `.` in the dotted path.
Example: `KB_QDRANT__URL` → `qdrant.url`, `KB_CHUNKING__MAX_CHARS` → `chunking.max_chars`,
`KB_SOURCES__HERMES__ENABLED` → `sources.hermes.enabled`.

### Global Knobs

| Knob | Env var | Default |
|---|---|---|
| `state_dir` | `KB_STATE_DIR` | `~/.digital-twins` |
| `config_dir` | `KB_CONFIG_DIR` | `~/.config/digital-twins` |

### Endpoint Knobs

User-supplied; `digital-twins init` prompts for these. No built-in defaults.

| Knob | Env var |
|---|---|
| `qdrant.url` | `KB_QDRANT__URL` |
| `qdrant.api_key` | `KB_QDRANT__API_KEY` |
| `neo4j.url` | `KB_NEO4J__URL` |
| `neo4j.user` | `KB_NEO4J__USER` |
| `neo4j.password` | `KB_NEO4J__PASSWORD` |
| `llm.endpoint` | `KB_LLM__ENDPOINT` |
| `llm.model` | `KB_LLM__MODEL` |
| `llm.api_key` | `KB_LLM__API_KEY` |

### Embedding Knobs

| Knob | Env var | Default |
|---|---|---|
| `embedding.model` | `KB_EMBEDDING__MODEL` | `BAAI/bge-small-en-v1.5` (384-dim; version pinned) |
| `embedding.device` | `KB_EMBEDDING__DEVICE` | `auto` (`auto`\|`cpu`\|`cuda`) |

### Chunking Knobs

| Knob | Env var | Default |
|---|---|---|
| `chunking.max_chars` | `KB_CHUNKING__MAX_CHARS` | 800 |
| `chunking.overlap` | `KB_CHUNKING__OVERLAP` | 100 |

### Source Knobs

Built-in sources: `hermes`, `pi`, `dsh`, `paperclip`, `yahoo`, `gmail`, `fs`.
All disabled on a fresh install. Each source supports:

| Knob | Default | Notes |
|---|---|---|
| `sources.<name>.enabled` | `false` | enable/disable |
| `sources.<name>.max_items` | 200 | per-run cap |
| `sources.<name>.timeout_s` | 1500 | per-source timeout |
| `sources.<name>.extra.*` | source-specific | see below |

Per-source `extra` fields:

| Source | `extra` field | Purpose |
|---|---|---|
| `hermes` | `state_db` | readable hermes session store |
| `pi` | `sessions_dir` | readable pi sessions dir |
| `dsh` | `sessions_dir` | readable dsh sessions dir |
| `paperclip` | `pg_dsn` | paperclip Postgres DSN |
| `yahoo` | `imap_host` | IMAP host (default `imap.mail.yahoo.com`) |
| `gmail` | `imap_host` | IMAP host (default `imap.gmail.com`) |
| `fs` | `dir` | directory of files (demo/test source) |

Credential env vars (referenced by name in config; values never in `kb.yml`):

| Source | Env var |
|---|---|
| `yahoo` | `YMAIL_APP_PASSWORD` |
| `gmail` | `GMAIL_APP_PASSWORD` |

### Custom Sources

User-defined sources ship no package code. Add a block to `kb.yml`:

```yaml
sources:
  mytool:
    enabled: true
    entrypoint: "mytool_kb:make_source"   # importable module:factory
    credential: "MYTOOL_TOKEN"            # env-var name the source requires
    prefix: "mytool:"                     # stamped onto source_url
```

| Knob | Purpose |
|---|---|
| `sources.<name>.entrypoint` | `module:factory` import path |
| `sources.<name>.credential` | env-var name (capability declaration) |
| `sources.<name>.prefix` | `source_url` prefix (default `<name>:`) |

### Debugging Config Resolution

Use `load_debug()` to see which layer supplied each resolved value:

```python
from digital_twins.config.loader import load_debug

result = load_debug()
print(result["chunking.max_chars"])  # "kb.local.yml"
```

Each key maps to the layer name (`"env"`, `"kb.local.yml"`, `"kb.yml"`, or `"default"`)
that supplied the final value.
