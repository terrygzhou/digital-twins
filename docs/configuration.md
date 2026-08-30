# Configuration reference

The single authoritative, human-readable reference for every configuration
knob the `digital-twins` package understands. The machine-readable single
source of truth is the `KNOBS` registry in `digital_twins/config/knobs.py`;
a standing guard (`tests/unit/test_knob_docs.py`) verifies that this page
stays in lock-step with it — every knob below has a row, no row here names
a knob that does not exist, and each row's default and env var match the
registry.

## How the config loads: precedence

Values resolve through a deterministic four-file precedence, highest wins:

1. **Environment variables** (including any `.env` file in the working
   directory) — highest priority.
2. **`kb.local.yml`** (in `config_dir`) — per-host overrides; never commit
   it.
3. **`kb.yml`** (in `config_dir`) — shared project config; safe to commit.
4. **Built-in defaults** — the values in the *default* column below.

Environment variables beat `kb.local.yml`, which beats `kb.yml`, which beats
the built-in default. A knob you set in no layer falls back to its built-in
default.

## How env vars map to knobs

- Every config knob has an env var with the `KB_` prefix.
- The knob path, upper-cased, forms the body; each nesting level (the `.`)
  becomes a `__` separator.
- Examples:

  | Knob | Env var |
  |---|---|
  | `state_dir` | `KB_STATE_DIR` |
  | `qdrant.url` | `KB_QDRANT__URL` |
  | `scheduler.status_port` | `KB_SCHEDULER__STATUS_PORT` |
  | `sources.yahoo.enabled` | `KB_SOURCES__YAHOO__ENABLED` |

- A row whose env var is `~` has **no** env-var mapping; set it only in a
  config file. A row whose default is `~` has **no** built-in default
  (the knob is unset until you provide a value, and a missing required
  endpoint fails fast at validate time).

## Global

| knob | type | default | env var | notes |
|---|---|---|---|---|
| state_dir | str | ~/.digital-twins | KB_STATE_DIR | Where runtime state (the SQLite database and checkpoints) lives. |
| config_dir | str | ~/.config/digital-twins | KB_CONFIG_DIR | Where `kb.yml` / `kb.local.yml` are looked up. |

## Endpoints

All endpoint knobs are optional and unset by default; a `digital-twins
validate` run fails fast naming any endpoint an enabled source needs that
has not been supplied.

**No-GPU / external-LLM path (BR-12.3.4).** The `llm.endpoint` knob is the
way to point at an external LLM when no suitable GPU is present. On a
no-GPU host the local bootstrap (`scripts/bootstrap-local.sh`) skips the
bundled `llm` service and prints `llm: SKIPPED (no suitable GPU — set
KB_LLM__ENDPOINT to an external LLM via config or the admin UI)`; set
`llm.endpoint` (or `KB_LLM__ENDPOINT`) to an OpenAI-compatible or SGLang
server URL to run the full stack against that external endpoint.

| knob | type | default | env var | notes |
|---|---|---|---|---|
| qdrant.url | str | ~ | KB_QDRANT__URL | Vector store endpoint URL. |
| qdrant.api_key | str | ~ | KB_QDRANT__API_KEY | Qdrant auth token, when the deployment requires one. |
| neo4j.url | str | ~ | KB_NEO4J__URL | Graph database endpoint URL. |
| neo4j.user | str | ~ | KB_NEO4J__USER | Neo4j login user. |
| neo4j.password | str | ~ | KB_NEO4J__PASSWORD | Neo4j login password. |
| llm.endpoint | str | ~ | KB_LLM__ENDPOINT | LLM server URL (e.g. an SGLang server). |
| llm.model | str | ~ | KB_LLM__MODEL | LLM model identifier. |
| llm.api_key | str | ~ | KB_LLM__API_KEY | LLM auth token, when the endpoint requires one. |

## Embedding

| knob | type | default | env var | notes |
|---|---|---|---|---|
| embedding.model | str | BAAI/bge-small-en-v1.5 | KB_EMBEDDING__MODEL | Text-embedding model; the default is pinned by the package. |
| embedding.device | str | auto | KB_EMBEDDING__DEVICE | One of `auto`, `cpu`, `cuda`. `auto` probes the host. |
| embedding.endpoint | str | ~ | KB_EMBEDDING__ENDPOINT | Optional hosted-embedding OpenAI-compatible endpoint (the reference compose ships one). |
| embedding.api_key | str | ~ | KB_EMBEDDING__API_KEY | Bearer token for `embedding.endpoint`, when it requires one. |

## Chunking

| knob | type | default | env var | notes |
|---|---|---|---|---|
| chunking.max_chars | int | 800 | KB_CHUNKING__MAX_CHARS | Maximum characters per chunk. |
| chunking.overlap | int | 100 | KB_CHUNKING__OVERLAP | Character overlap between adjacent chunks; must be less than `max_chars`. |

## Scheduler

| knob | type | default | env var | notes |
|---|---|---|---|---|
| scheduler.status_port | int | 8765 | KB_SCHEDULER__STATUS_PORT | TCP port for the scheduler status server; `0` disables the server. |
| mcp.port | int | 8770 | KB_MCP__PORT | TCP port for the MCP (kb-mcp) service. |
| mcp.service_account_email | str | system | KB_MCP__SERVICE_ACCOUNT_EMAIL | Service-account address the MCP service runs as. |

## Web

| knob | type | default | env var | notes |
|---|---|---|---|---|
| web.bind | str | 127.0.0.1 | KB_WEB__BIND | Address the web UI server binds to; `127.0.0.1` keeps it reachable only from the local host. |
| web.port | int | 8767 | KB_WEB__PORT | TCP port for the web UI server. |
| web.base_url | str | http://localhost:8767 | KB_WEB__BASE_URL | Public URL of the web UI, e.g. for embedding links in tool output or dashboards. |

## Sources

All built-in sources ship **disabled** on a fresh install
(`enabled: false`). The three rows per source (`enabled`, `max_items`,
`timeout_s`) apply to every built-in source; `max_items` caps items per
run and `timeout_s` bounds a single source's run.

| knob | type | default | env var | notes |
|---|---|---|---|---|
| sources.hermes.enabled | bool | false | KB_SOURCES__HERMES__ENABLED | Enable the Hermes source. |
| sources.hermes.max_items | int | 200 | KB_SOURCES__HERMES__MAX_ITEMS | Per-run item cap for Hermes. |
| sources.hermes.timeout_s | int | 1500 | KB_SOURCES__HERMES__TIMEOUT_S | Per-run timeout (seconds) for Hermes. |
| sources.pi.enabled | bool | false | KB_SOURCES__PI__ENABLED | Enable the pi source. |
| sources.pi.max_items | int | 200 | KB_SOURCES__PI__MAX_ITEMS | Per-run item cap for pi. |
| sources.pi.timeout_s | int | 1500 | KB_SOURCES__PI__TIMEOUT_S | Per-run timeout (seconds) for pi. |
| sources.dsh.enabled | bool | false | KB_SOURCES__DSH__ENABLED | Enable the dsh source. |
| sources.dsh.max_items | int | 200 | KB_SOURCES__DSH__MAX_ITEMS | Per-run item cap for dsh. |
| sources.dsh.timeout_s | int | 1500 | KB_SOURCES__DSH__TIMEOUT_S | Per-run timeout (seconds) for dsh. |
| sources.paperclip.enabled | bool | false | KB_SOURCES__PAPERCLIP__ENABLED | Enable the Paperclip source. |
| sources.paperclip.max_items | int | 200 | KB_SOURCES__PAPERCLIP__MAX_ITEMS | Per-run item cap for Paperclip. |
| sources.paperclip.timeout_s | int | 1500 | KB_SOURCES__PAPERCLIP__TIMEOUT_S | Per-run timeout (seconds) for Paperclip. |
| sources.yahoo.enabled | bool | false | KB_SOURCES__YAHOO__ENABLED | Enable the Yahoo Mail source. |
| sources.yahoo.max_items | int | 200 | KB_SOURCES__YAHOO__MAX_ITEMS | Per-run item cap for Yahoo Mail. |
| sources.yahoo.timeout_s | int | 1500 | KB_SOURCES__YAHOO__TIMEOUT_S | Per-run timeout (seconds) for Yahoo Mail. |
| sources.gmail.enabled | bool | false | KB_SOURCES__GMAIL__ENABLED | Enable the Gmail source. |
| sources.gmail.max_items | int | 200 | KB_SOURCES__GMAIL__MAX_ITEMS | Per-run item cap for Gmail. |
| sources.gmail.timeout_s | int | 1500 | KB_SOURCES__GMAIL__TIMEOUT_S | Per-run timeout (seconds) for Gmail. |
| sources.fs.enabled | bool | false | KB_SOURCES__FS__ENABLED | Enable the filesystem demo source (no credentials needed). |
| sources.fs.max_items | int | 200 | KB_SOURCES__FS__MAX_ITEMS | Per-run item cap for the filesystem source. |
| sources.fs.timeout_s | int | 1500 | KB_SOURCES__FS__TIMEOUT_S | Per-run timeout (seconds) for the filesystem source. |
| sources.yahoo.credential | str | ~ | YMAIL_APP_PASSWORD | Env var holding the Yahoo app-password credential (required when enabled). |
| sources.yahoo.email | str | ~ | YMAIL_EMAIL | Env var holding the Yahoo account address. |
| sources.gmail.credential | str | ~ | GMAIL_APP_PASSWORD | Env var holding the Gmail app-password credential (required when enabled). |
| sources.gmail.email | str | ~ | GMAIL_EMAIL | Env var holding the Gmail account address. |
| sources.mytool.enabled | bool | false | ~ | Custom-source example (`mytool`): enable the source. |
| sources.mytool.entrypoint | str | ~ | ~ | Custom-source example: `module:factory` entry point to import. |
| sources.mytool.credential | str | ~ | MYTOOL_TOKEN | Env var name the custom source requires for its credential. |
| sources.mytool.prefix | str | mytool: | ~ | ID prefix stamped on items this source ingests. |

### Adding a source (custom)

`sources.mytool.*` is the documented custom-source example. A custom source
adds `enabled`, `entrypoint` (a `module:factory` import path),
`credential` (the env-var name it needs), and `prefix` under its own name
in `kb.yml`; see the *Add a new source* section of `README.md`.

## Deprecation mechanism

005 documents the mechanism; no real knob is deprecated as of this release.

When a knob is deprecated, the package keeps reading it for one release
while emitting a **one-run warning** (warning, not error, not fatal) that
names the deprecated knob and its replacement, then the knob is removed in
the following major release. The replacement is recorded in `CHANGELOG.md`,
and the deprecation lands as a minor bump with the breaking removal landing
as a major (see `docs/semver-policy.md`).

The behavior is pinned by a test fixture in `tests/` (not by this doc): a
config that sets the deprecated knob must produce the warning naming the
replacement on the first run and still behave as the replacement does.
Any knob deprecation must keep the `KNOBS` registry, this page,
`config.example.yml`, and `.env.example` in lock-step, or the standing
guards (`test_knob_docs.py`, `test_portability.py`) fail.
