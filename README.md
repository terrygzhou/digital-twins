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

| Command | Flags | Description |
|---|---|---|
| `digital-twins --version` | | Print version and exit |
| `digital-twins --version-json` | | Print machine-readable version (JSON) and exit |
| `digital-twins init` | `--yes` | First-run setup: endpoints, starter `kb.local.yml`, state DB, health report |
| `digital-twins validate` | | Health-check the configured endpoints; exits 0 only when all pass |
| `digital-twins run` | `--source NAME`, `--max-items N`, `--dry-run` | One-shot ingestion: read → chunk → embed → upsert |

All commands are implemented. The full flag set is verified against
[`digital_twins/cli.py`](digital_twins/cli.py).

## Quickstart

### 1 — Install

```bash
pip install -e .
```

Requires Python ≥ 3.11. See [Install](#install) above for the
`--system-site-packages` variant.

### 2 — Initialise

```bash
digital-twins init        # prompts for qdrant.url, neo4j.url/user, llm.endpoint
```

**Expected**: config dir created with `kb.local.yml` (every source `enabled: false`);
state dir + `state.db` created; health table printed; exit 0.
Re-running `init` keeps existing values, prompts for nothing new, exits 0 (idempotent).

### 3 — Validate

```bash
digital-twins validate
```

**Expected**: exit 0, all endpoints ok.
Point `qdrant.url` at a collection with a different vector size →
**hard error** naming the mismatch plus the remediation ("re-embed, or point at a new collection"), exit 1.

### 4 — Ingest + idempotent re-run

```bash
# enable the fs source on a directory of two .md files in kb.local.yml:
#   sources.fs: { enabled: true, extra: { dir: /tmp/kb-demo } }
digital-twins run --source fs
digital-twins run --source fs      # second run
```

**Expected**: run 1 → `fs: 2 item(s)`, audit row with `run_id` written;
run 2 → `fs: 0 item(s)`; the collection still holds exactly 2 points.

### 5 — Fail-fast prerequisite

```bash
# kb.local.yml: sources.hermes.enabled: true (no session store present on this host)
digital-twins run --source hermes
```

**Expected**: exit **2**, message names the source + missing prerequisite + where to set it.
Nothing ingested; the failed run is still audited.

### 6 — Custom source without a package update

```yaml
# kb.local.yml
sources.mytool:
  enabled: true
  entrypoint: mytool_kb:make_source     # user module on PYTHONPATH
  credential: MYTOOL_TOKEN
  prefix: "mytool:"
```

**Expected**: with `MYTOOL_TOKEN` set, `digital-twins run --source mytool` ingests;
without it, fail-fast exit 2 naming the credential. No package reinstall involved.

### 7 — Portability + knob-doc audit

```bash
pytest tests/integration/test_portability.py tests/unit/test_knob_docs.py
```

**Expected**: both pass — zero host-specific paths/usernames in shipped code, config, docs;
zero undocumented knobs.

## Validation Guide

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (all health checks pass; ingestion completed) |
| `1` | Health check failure or Qdrant dimension mismatch |
| `2` | Prerequisite failure (missing credential, missing source, etc.) |

### Test commands

```bash
pytest tests/unit -q                       # hermetic default suite
pytest tests/integration -q                # in-memory Qdrant + stubs
KB_LIVE_QDRANT=... KB_LIVE_NEO4J=... pytest -m live   # opt-in real services
```

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
| `sources.yahoo.credential` / `sources.gmail.credential` | provider app-password env-var name | env-var holding the required secret (IMAP sources) |
| `sources.yahoo.email` / `sources.gmail.email` | `""` | IMAP account address; empty = use the provider `*_EMAIL` env var |

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

Credential / account env vars (referenced by name in config; values never in `kb.yml`):

| Source | Env var | Purpose |
|---|---|---|
| `yahoo` | `YMAIL_APP_PASSWORD` | required IMAP app password (secret) |
| `gmail` | `GMAIL_APP_PASSWORD` | required IMAP app password (secret) |
| `yahoo` | `YMAIL_EMAIL` | IMAP account address (used when `sources.yahoo.email` is empty) |
| `gmail` | `GMAIL_EMAIL` | IMAP account address (used when `sources.gmail.email` is empty) |

### Custom Sources

User-defined sources ship no package code — you write a small Python module
that fulfils the source contract, and the pipeline imports and drives it.
The full contract lives in [`digital_twins/sources/base.py`](digital_twins/sources/base.py)
and [`specs/001-package-foundation/contracts/source.md`](specs/001-package-foundation/contracts/source.md).

#### The Contract

Your module must export a **factory function**:

```python
def make_source(entry: dict) -> Source:
    ...
```

The factory receives the config entry dict (everything under `sources.<name>`
in `kb.local.yml`, plus a `name` key). It must return a `Source` instance
with:

| Member | Type | Purpose |
|---|---|---|
| `name` | `str` | short identifier (usually the config key) |
| `capability` | `Capability` | declared runtime / credential / prefix |
| `prerequisites()` | `-> list[str]` | human-readable list of **missing** prerequisites; empty = ready. Must be connection-free (no I/O). |
| `read(since)` | `-> Iterator[IngestItem]` | yield items newer than the high-water cursor `since` (`str` or `None`). Must be resumable: re-reading after interruption yields the same items with stable keys. |
| `close()` | `-> None` | release any resources (connections, handles). |

#### Capability

```python
Capability(runtime, credential, prefix)
```

| Field | Type | Purpose |
|---|---|---|
| `runtime` | `str \| None` | agent runtime this source reads (e.g. `"hermes"`, `"pi"`); `None` = host-neutral |
| `credential` | `str \| None` | env-var name holding the required secret (e.g. `"MYTOOL_TOKEN"`); `None` = no credential |
| `prefix` | `str` | stamped onto `source_url` (e.g. `"mytool:"`) |

#### IngestItem

```python
IngestItem(key, content, ts, metadata)
```

| Field | Type | Purpose |
|---|---|---|
| `key` | `str` | stable unique identifier (feeds the deterministic point ID; used for dedup) |
| `content` | `str` | the text to chunk and embed |
| `ts` | `str` | ISO-8601 timestamp (drives the high-water cursor) |
| `metadata` | `dict` | optional extra fields (defaults to `{}`) |

#### Minimal Working Example

`mytool_kb.py` (place on your `PYTHONPATH` or in the same directory as the config):

```python
import os
from digital_twins.sources.base import Source, Capability, IngestItem


class MyToolSource(Source):
    name = "mytool"
    capability = Capability(
        runtime="mytool",
        credential="MYTOOL_TOKEN",
        prefix="mytool:",
    )

    def prerequisites(self) -> list[str]:
        missing = []
        if not os.environ.get("MYTOOL_TOKEN"):
            missing.append("MYTOOL_TOKEN is not set")
        return missing

    def read(self, since):
        # `since` is the last high-water cursor (ISO-8601 string or None).
        # Replace the body with your actual fetch logic; yield dicts with
        # at least "id", "text", and "updated_at".
        for item in self._fetch(since):
            yield IngestItem(
                key=item["id"],
                content=item["text"],
                ts=item["updated_at"],
                metadata={"author": item.get("author")},
            )

    def _fetch(self, since):
        # Replace with your actual fetch logic. Return an iterable of dicts,
        # each with "id" (str), "text" (str), "updated_at" (ISO-8601 str).
        return []

    def close(self):
        pass  # nothing to release


def make_source(entry):
    return MyToolSource()
```

#### Configuration

Add a block to `kb.local.yml` (or `kb.yml`):

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

#### Fail-Fast Behaviour

The pipeline validates your source before ingesting anything:

| Failure | Behaviour |
|---|---|
| Module cannot be imported | `CustomSourceError` naming the module path and the original import error |
| Factory attribute missing or not callable | `CustomSourceError` naming the expected contract |
| Factory returns a non-`Source` | `CustomSourceError` naming the returned type and the expected `Source` |
| `Source` missing a required method (`prerequisites`, `read`, `close`) | `CustomSourceError` naming the missing method |
| `capability` is not a `Capability` instance | `CustomSourceError` naming the expected type |
| Declared credential env var is missing | `prerequisites()` reports it; `digital-twins run` exits with status 2 |

Nothing is ingested until all prerequisites pass.

#### Testing

See [`tests/unit/test_custom_source.py`](tests/unit/test_custom_source.py)
for test patterns: valid entrypoint, contract adherence, import failure,
non-Source return, missing methods, and credential env-var checks.

### Debugging Config Resolution

Use `load_debug()` to see which layer supplied each resolved value:

```python
from digital_twins.config.loader import load_debug

result = load_debug()
print(result["chunking.max_chars"])  # "kb.local.yml"
```

Each key maps to the layer name (`"env"`, `"kb.local.yml"`, `"kb.yml"`, or `"defaults"`)
that supplied the final value.
