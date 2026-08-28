# Config Schema (documented knobs)

The stable, documented config schema (BR-11.6.4). Precedence: **env (incl. `.env`) > `kb.local.yml` > `kb.yml` > built-in defaults**. Env vars: `KB_` prefix, `__` = nesting (e.g. `KB_QDRANT__URL`). `kb.local.yml` is machine-local (untracked); `kb.yml` holds committed defaults. No knob may exist that is not in this table — enforced by the `knobs.py` ↔ example-files sync test (SC-002).

## Global

| Knob | Env var | Default | Notes |
|---|---|---|---|
| `state_dir` | `KB_STATE_DIR` | `~/.digital-twins` | state DB + flags |
| `config_dir` | `KB_CONFIG_DIR` | `~/.config/digital-twins` | kb.yml, kb.local.yml |

## Endpoints

| Knob | Env var | Default |
|---|---|---|
| `qdrant.url` | `KB_QDRANT__URL` | *(unset — init prompts)* |
| `qdrant.api_key` | `KB_QDRANT__API_KEY` | *(unset)* |
| `neo4j.url` | `KB_NEO4J__URL` | *(unset)* |
| `neo4j.user` | `KB_NEO4J__USER` | *(unset)* |
| `neo4j.password` | `KB_NEO4J__PASSWORD` | *(unset)* |
| `llm.endpoint` | `KB_LLM__ENDPOINT` | *(unset)* |
| `llm.model` | `KB_LLM__MODEL` | *(unset)* |
| `llm.api_key` | `KB_LLM__API_KEY` | *(unset)* |

## Embedding

| Knob | Env var | Default |
|---|---|---|
| `embedding.model` | `KB_EMBEDDING__MODEL` | `BAAI/bge-small-en-v1.5` (384-dim; model version pinned) |
| `embedding.device` | `KB_EMBEDDING__DEVICE` | `auto` (`auto`\|`cpu`\|`cuda`) |

## Chunking

| Knob | Env var | Default |
|---|---|---|
| `chunking.max_chars` | `KB_CHUNKING__MAX_CHARS` | 800 |
| `chunking.overlap` | `KB_CHUNKING__OVERLAP` | 100 |

## Sources

`sources.<name>` — built-in names: `hermes`, `pi`, `dsh`, `paperclip`, `yahoo`, `gmail`, `fs`.

| Knob | Default | Notes |
|---|---|---|
| `sources.<name>.enabled` | `false` | **all disabled on a fresh install** |
| `sources.<name>.max_items` | 200 | per-run cap (baseline 200-mail cap preserved as a per-source knob) |
| `sources.<name>.timeout_s` | 1500 | per-source timeout |
| `sources.<name>.extra.*` | source-specific | hermes/pi/dsh: session-store paths; paperclip: PG DSN; yahoo/gmail: IMAP host |
| `sources.<custom>.entrypoint` | — | `module:factory` for user-defined sources (FR-011) |
| `sources.<custom>.credential` | — | env-var name the source requires (capability declaration) |
| `sources.<custom>.prefix` | `<name>:` | stamped onto `source_url` |

Credential env vars (e.g. `YMAIL_APP_PASSWORD`, `GMAIL_APP_PASSWORD`) are referenced **by name** in config and resolved from env/`.env` — secret values never live in `kb.yml` (NFR-11 hygiene).
