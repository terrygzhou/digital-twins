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

## Setup is separate and idempotent

Installing and configuring are two separate steps. The installer
(`scripts/install.sh` / `scripts/install-local.sh`) is **install-only by
default**: it stops after `pip install` and prints `install-only … run
digital-twins setup`. It does **not** run the setup wizard unless you opt
in. `digital-twins setup` is the first-run wizard — backend detection,
init, first admin account, and health checks in one command — and is
**idempotent**: re-running it on a host that is already installed and
configured is a safe no-op.

- **Install-only default.** The installer stops after the `pip install`
  and prints the install-only note. Re-running the installer on an
  already-installed host is a safe no-op.
- **`--with-setup` (opt-in).** Chains the installer + setup wizard in one
  command (the install finishes, then the wizard runs).
- **`--no-setup` (no-op alias, one release).** The installer is
  install-only by default, so there is nothing to opt out of; the flag is
  accepted but does nothing.
- **`digital-twins setup` (idempotent).** Re-running on an already
  configured host is a no-op; it does not reset your config.
- **`digital-twins init` (deprecated alias).** Runs the *narrower* subset
  of `setup` (state DB + migrations + first admin + health report, not the
  backend decision). `--yes` is preserved. Prefer `digital-twins setup`.

### Per-service backend choice

Each of the four services — qdrant, neo4j, llm, embedding — can be served
by the bundled local Docker stack (`local`) or by an external cloud
endpoint (a URL you point at via env var). Services not named in
`--backends` default to `local`; if every service ends up external the
local stack is not started:

| Service   | Local (Docker)                           | External (cloud URL) via env var |
|-----------|------------------------------------------|----------------------------------|
| qdrant    | `local` (qdrant/qdrant:1.9.7)           | `KB_QDRANT__URL`               |
| neo4j     | `local` (neo4j/neo4j:5.18-community)    | `KB_NEO4J__URL` / `KB_NEO4J__USER` / `KB_NEO4J__PASSWORD` |
| llm       | `local` (bundled, GPU required)         | `KB_LLM__ENDPOINT` / `KB_LLM__MODEL` |
| embedding | `local` (BAAI/bge-small-en-v1.5, 384-dim) | `KB_EMBEDDING__ENDPOINT` / `KB_EMBEDDING__API_KEY` |

Pick the whole local stack at once with `--local` (starts all four
services; skips Docker detection and the cloud prompts):

```bash
digital-twins setup --local
```

Pick per-service backends with `--backends KEY=VAL,...`, where each `VAL`
is `local` (the bundled Docker service) or an external URL (placeholders
below — use your own endpoints):

```bash
# qdrant from Docker, llm + embedding from a cloud endpoint,
# neo4j not named → defaults to local.
digital-twins setup --backends \
    qdrant=local,llm=https://example.com/v1,embedding=https://example.com/v1
```

`--local` and `--backends` are **`digital-twins setup` flags, not
installer flags.** Flag precedence (highest wins): `--skip-services` >
`--cloud-env` > `--cloud` > `--local` / `--backends` > interactive.

### Exit codes (with `--with-setup`)

With `--with-setup`, the installer's exit code mirrors the setup wizard's
exit code: `0` all checks pass · `3` the local stack's mandatory services
did not become healthy · `5` a cloud endpoint could not be resolved · `6`
the wizard was interrupted before the backend was configured.

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
| qdrant.collection | str | personal_kb | KB_QDRANT__COLLECTION | Qdrant collection name for all vector ops; override to target a different collection on the same host. |
| neo4j.url | str | ~ | KB_NEO4J__URL | Graph database endpoint URL. |
| neo4j.user | str | ~ | KB_NEO4J__USER | Neo4j login user. |
| neo4j.password | str | ~ | KB_NEO4J__PASSWORD | Neo4j login password. |
| llm.endpoint | str | ~ | KB_LLM__ENDPOINT | LLM server URL (e.g. an SGLang server). |
| llm.model | str | ~ | KB_LLM__MODEL | LLM model identifier. |
| llm.api_key | str | ~ | KB_LLM__API_KEY | LLM auth token, when the endpoint requires one. |


### S4 graph and Qdrant payload (s4-graph-alignment)

When `neo4j.url`, `neo4j.user`, and `neo4j.password` are all set, every
ingest surface writes a `(:SourceItem {item_id, channel, content_hash})` node
per ingested item (deduped on `item_id`; no-op when already present).
Legacy `:KbItem` / `:KbChunk` / `HAS_CHUNK` writes were removed in this
change; run `digital-twins migrate s4` on upgraded installs to clean up
old nodes.

**Qdrant payload fields (S4-aligned, s4-graph-alignment task 2):**

| field | description |
|---|---|
| `item_id` | `item.key` — the join key into `SourceItem.item_id` |
| `content_hash` | item-level (shared by all chunks of one item); differs from chunk-level `content_hash()` used pre-S4 |
| `full_content` | the chunk's raw text (NOT stored in Neo4j; Qdrant is the text store) |
| `content_snippet` | `full_content[:200]` |
| `captured_at` | the item's `ts` (rename of pre-S4 `ts` field) |
| `total_chunks` | number of chunks produced for this item |
| `embed_model` | the embedding model name (from `embedding.model` config) |
| `source_type` | the source name (e.g. `fs`, `hermes`, `pi`) |
| `tags` | `[]` (placeholder; populated by future changes) |
| `meta.owner` / `meta.owner_tag` | owner fields moved under `meta` (003 multi-user) |
| `run_id` / `trigger` | optional provenance passthroughs; when written they equal the audit row's values |

**Recorded decisions (s4-graph-alignment task 6.3):**

(a) **NFR-1 dedup is within-system.** The same item ingested via schedule,
`run --once`, MCP, or web UI yields one Qdrant point (content-independent
point ID under the S4 scheme). Cross-system content dedup (the same
underlying document reaching digital-twins and a separate knowledge-base
system that shares the `personal_kb` Qdrant collection) is out of scope:
it requires a channel-mapping table (this package's source name → the
other system's channel), which is blocked on the other system's
channel-registry ACL. Revisit if that ACL is made available.

(b) **Chunk text lives in Qdrant, not Neo4j.** The Neo4j graph stores
`(:SourceItem)` / `(:Entity)` / `:MENTIONED` / `:REL` nodes and edges
only; the full chunk text is in Qdrant's `full_content` payload field.
Neo4j = entity/relation graph; Qdrant = vector + payload store.

**After upgrading from a pre-S4 install:** run `digital-twins migrate s4`
to remove legacy `:KbItem` / `:KbChunk` nodes, then re-ingest — old
points are orphans under the new content-independent point-ID scheme.

## Embedding

| knob | type | default | env var | notes |
|---|---|---|---|---|
| embedding.model | str | BAAI/bge-small-en-v1.5 | KB_EMBEDDING__MODEL | Text-embedding model; the default is pinned by the package. In-process (no `embedding.endpoint`) the model must be a pinned name; with an external `embedding.endpoint` a custom name is accepted (the endpoint owns its model; the dimension guard degrades to pass-through). |
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

## Extraction

LLM entity extraction (s4-entity-extraction), config-gated: when enabled,
every ingest surface that has a Neo4j driver runs an LLM entity extraction
step after the `:SourceItem` graph write. Requires `llm.endpoint` to be set.
Default: disabled (no LLM calls, no entity writes).

| knob | type | default | env var | notes |
|---|---|---|---|---|
| extraction.enabled | bool | false | KB_EXTRACTION__ENABLED | Run LLM entity extraction after the `:SourceItem` graph write. |
| extraction.max_text_chars | int | 12000 | KB_EXTRACTION__MAX_TEXT_CHARS | Truncation cap (characters) for the text handed to the LLM. |
| extraction.prompt_version | str |  | KB_EXTRACTION__PROMPT_VERSION | Prompt version override; empty uses the built-in prompt. |

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

### Managing channels (CLI)

The `digital-twins channels` command group manages ingestion channels
(sources) — list / status / enable / disable / add. Read-only views
(`list`, `status`) require no authentication (mirrors the `validate` and
`run` pattern); writes (`enable`, `disable`, `add`) merge into
`kb.local.yml` only — `kb.yml` is never written.

> **Note on env overrides:** The `KB_SOURCES__<NAME>__*` environment
> variables shadow any values saved in `kb.local.yml` (env wins per
> the four-layer precedence). Use `channels list` to see the effective
> values after env resolution.

#### `channels list`

Table of every registered channel (built-ins + custom sources registered
in config), one row per channel:

| column | meaning |
|---|---|
| name | source name |
| enabled | `yes` / `no` (effective) |
| max_items | effective per-run cap |
| timeout_s | effective per-run timeout (seconds) |
| credential | `set` / `not-set` (booleans only — never the value) |
| prerequisites | `BLOCKED: <comma-joined missing prerequisites>` when the channel has unsatisfied prerequisites, or `-` when ready |

```
$ digital-twins channels list
name          enabled  max_items  timeout_s  credential  prerequisites
dsh           no       200        1500       set         BLOCKED: dsh sessions_dir not set
fs            no       200        1500       set         -
gmail         no       200        1500       not-set     BLOCKED: GMAIL_APP_PASSWORD not set
hermes        yes      50         300        set         -
pi            no       200        1500       set         BLOCKED: pi sessions_dir not set
paperclip     no       200        1500       set         BLOCKED: paperclip sessions_dir not set
yahoo         no       200        1500       not-set     BLOCKED: YMAIL_APP_PASSWORD not set
```

#### `channels status <name>`

Shows the detail row for one channel, including prerequisites. Exits 1
with a named reason if the source is not registered (fail-fast,
BR-11.2.2):

```
$ digital-twins channels status gmail
name     enabled  max_items  timeout_s  credential  prerequisites
gmail    no       200        1500       not-set     BLOCKED: GMAIL_APP_PASSWORD not set
```

#### `channels enable <name> [--max-items N] [--timeout-s N]`

Writes `{"enabled": true}` (and optional `max_items` / `timeout_s`) into
`kb.local.yml` via `local_io.channel_write`. Exits 1 with a schema
error if the source is unknown.

```
$ digital-twins channels enable fs --max-items 100 --timeout-s 60
enabled channel `fs` -> $CONFIG_DIR/kb.local.yml
```

#### `channels disable <name>`

Writes `{"enabled": false}` into `kb.local.yml`. No flags.

```
$ digital-twins channels disable hermes
disabled channel `hermes` -> $CONFIG_DIR/kb.local.yml
```

#### `channels add <name> --entrypoint module:factory [--credential ENV_NAME] [--prefix P]`

Registers a custom channel (disabled by default). The entrypoint is
import-checked before writing: if the module does not import, the factory
attribute is missing, or the factory is not callable, the command exits
1 with a named `CustomSourceError` and **nothing is written**.

```
$ digital-twins channels add mytool --entrypoint mypackage.factory:build_mytool \
    --credential MYTOOL_TOKEN --prefix mytool:
added channel `mytool` (disabled) -> $CONFIG_DIR/kb.local.yml
```

---

### Web admin API: /api/config/channels

Both endpoints are admin-gated (same gate as `/api/config/services`).
Non-admin requests receive 403.

#### GET /api/config/channels

Returns 200 with the masked channel view:

```json
{
  "sources": {
    "fs": {
      "enabled": false,
      "max_items": 200,
      "timeout_s": 1500,
      "credential_set": true,
      "prerequisites": []
    }
  },
  "env_overrides": ["KB_SOURCES__FS__MAX_ITEMS"]
}
```

- `sources` — one entry per registered channel (built-ins + custom
  sources registered in config).
- `credential_set` — boolean only; **credential values are never
  returned** (FR-004 / BR-12.2.2).
- `prerequisites` — empty list when the channel is ready; one or more
  strings naming missing prerequisites or a broken entrypoint. In the
  CLI table a non-empty list renders as `BLOCKED: <names>` (spec:
  unsatisfied prerequisites are marked BLOCKED, never bare names).
- `env_overrides` — sorted list of `KB_SOURCES__<NAME>__*` (and
  per-source credential) env var names currently set that shadow a
  channel knob in the resolved config.

#### POST /api/config/channels

Body (non-empty mapping of source name → partial update):

```json
{
  "gmail": {"enabled": true, "max_items": 50},
  "hermes": {"timeout_s": 600}
}
```

On success (200), the response is the post-write masked view (same shape
as GET). The write goes to `kb.local.yml` via
`local_io.channel_write` (kb.yml is never written; unrelated keys are
preserved).

| status | condition |
|---|---|
| 200 | write succeeded; post-write masked view returned |
| 400 | body is not valid JSON |
| 403 | caller is not an admin |
| 404 | source name not in the channel view (including custom sources carrying an `entrypoint` — the web surface does not register custom channels; use `channels add` CLI) |
| 404 | unknown per-source knob (the web surface exposes only `enabled` / `max_items` / `timeout_s`) |
| 422 | value outside the knob's declared type or range (e.g. negative int); or a source value is not a mapping |
| 409 | existing `kb.local.yml` is unparseable or not a YAML mapping (no write performed) |
| 500 | write failed (permission denied / I/O error) |

> **Web admin UI:** The Channels panel in the web admin UI
> (`digital_twins/web/static/index.html`) is admin-only and mirrors the
> Services panel. It loads from GET and submits via POST; there is
> deliberately no add/registration UI — custom channel registration is a
> CLI `channels add` operation only.

---

### Per-user channel overrides

Each user account can hold per-channel overrides in the `user_config`
table. The scheduler (`run --as <user>`) resolves channel config in
this order, highest wins:

1. **User override** (`user_config` row for that user + source + key)
2. **Environment variables** (`KB_SOURCES__<NAME>__*`)
3. **`kb.local.yml`**
4. **`kb.yml`**
5. **Built-in defaults**

A user's `<name>.enabled: false` override suppresses that channel for
that user's scheduled runs only (SC-003 isolation — the global config
and other users' runs are unaffected).

**Read API:** `user_config.get_user_channel_overrides(db, user_id)`
returns `{<source_name>: {<key>: <value_text>}}` where the key is
limited to the overridable set: `enabled`, `max_items`, `timeout_s`.
Values are stored in TEXT form and type-coerced at merge time
(`merge_user_config`). A user with no overrides gets an empty dict.


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
