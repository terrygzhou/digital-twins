# digital-twins

Environment-portable KB ingestion: layered config, fail-fast named sources, and
deterministic dedup-safe ingest into user-supplied Qdrant + Neo4j. Built per
`specs/001-package-foundation/` (see `AGENTS.md` for sources of truth).

## Step-by-step onboarding (new machine, zero to first ingest)

Everything below runs on a fresh machine. Follow the steps in order — each
step tells you exactly what to type and what success looks like.

### Step 0 — Prerequisites

- **Python ≥ 3.11** (`python3 --version`)
- Either **Docker** (for the bundled local stack, Step 2) **or** already-running
  Qdrant + Neo4j + an OpenAI-compatible LLM + embedding endpoint you can point
  at.
- A GPU is optional — CPU-only hosts work (see the install note below).

### Step 1 — Install the package

```bash
# Create a venv (recommended: system Python on many distros is
# "externally managed" and refuses direct pip installs — PEP 668)
python3 -m venv .venv
. .venv/bin/activate

# CPU-only hosts: PyPI's default `torch` wheel is ~5 GB with CUDA bundled.
# Install the CPU wheel first, then the package:
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install digital-twins-kb
```

On a GPU host you can skip the torch line and just `pip install digital-twins-kb`.

From a git checkout instead of PyPI:

```bash
pip install .
```

**Check:**

```bash
digital-twins --version
# digital-twins, version 0.9.0
```

> **PyPI name:** the distribution is published as `digital-twins-kb`
> (`digital-twins` is blocked by PyPI's name-similarity policy); the CLI
> command stays `digital-twins` and the import package stays `digital_twins`.
> `python -m digital_twins` works too.

### Step 2 — Start the backend services (pick one)

The tool ingests into **your** Qdrant + Neo4j and uses an OpenAI-compatible
LLM + embedding endpoint. Three ways to get them:

**Option A — bundled local stack (Docker), one command:**

```bash
bash scripts/bootstrap-local.sh
```

Brings up qdrant (6333), neo4j (7474/7687), an LLM (8000) and an
embedding-model service (8080), and writes a machine-local `kb.local.yml`
pointing at them. On a no-GPU host the bundled `llm` is skipped — point
`KB_LLM__ENDPOINT` at an external LLM instead (see
[`docs/configuration.md`](docs/configuration.md) for the no-GPU path).
Tear down with `docker compose down`.
Exit codes: `0` healthy · `1` docker missing/down · `2` port conflict
(names the service) · `3` health timeout (prints `docker compose logs` hint).

> **Note:** run this with `bash`, not `sh`. The script uses
> `set -o pipefail` (line 19) which dash does not support; on systems where
> `sh` is dash, `sh scripts/bootstrap-local.sh` will fail immediately.

**Option B — cloud or external services (no Docker):**

```bash
bash scripts/bootstrap-local.sh --cloud
```

Set the required env vars (or answer the interactive prompts on stdin):

- `KB_QDRANT__URL` — Qdrant URL (e.g. `https://host:6333`)
- `KB_NEO4J__URL` — Neo4j URL (`bolt://` or `http(s)://`)
- `KB_LLM__ENDPOINT` — OpenAI-compatible LLM base URL

Optional: `KB_EMBEDDING__ENDPOINT`, `KB_NEO4J__USER`, `KB_NEO4J__PASSWORD`.

Writes `~/.config/digital-twins/kb.local.yml` pointing at the cloud
endpoints. No Docker required. Exit codes: `0` success · `5` a required
endpoint was not provided (names the missing variable(s)).

**Option C — your own services (manual):** skip the script and make sure
Qdrant, Neo4j, the LLM and the embedding endpoint are reachable; you'll tell
the tool where they live in Step 3.

### Step 3 — Initialise

```bash
digital-twins init
```


The wizard prompts, in order:

1. `qdrant.url` (e.g. `http://localhost:6333`)
2. `neo4j.url` / `neo4j.user` / `neo4j.password` (e.g. `bolt://localhost:7687`)
3. `llm.endpoint` / `llm.model` (OpenAI-compatible base URL + model name)
4. **first admin email + password** — this account is your `admin` role;
   every later sign-up is a `reader` (see [Roles & tokens](#roles--tokens))

It creates the config dir with `kb.local.yml` (every source `enabled: false`),
the state dir + `state.db`, and prints a health report.
**Check:** the report shows every service `ok` and the command exits 0.
If an endpoint is unreachable the command exits 1 and names the failing
service plus how to fix it — fix it and re-run `digital-twins init --yes`.
Re-running is safe (idempotent): existing values are kept, only missing
pieces are added.

### Step 4 — Validate

```bash
digital-twins validate
```

**Check:** exit 0, and every configured endpoint reachable with the Qdrant
collection vector size matching the embedding model. A dimension mismatch is
a hard error naming the mismatch and the remediation ("re-embed, or point at
a new collection"), exit 1.

### Step 5 — Enable a source and run your first ingest

Point a source at real content. The `fs` source is the simplest demo:

```bash
# In <config dir>/kb.local.yml (or via env: KB_SOURCES__FS__ENABLED=true etc.):
#   sources.fs: { enabled: true, extra: { dir: /tmp/kb-demo } }
# (put two .md files in /tmp/kb-demo first)
digital-twins run --source fs
```

**Check:** run 1 reports `fs: 2 item(s)` and writes an audit row with a
`run_id`. Run it again:

```bash
digital-twins run --source fs   # second run
```

Run 2 reports `fs: 0 item(s)` — the collection still holds exactly 2 points.
Dedup-safe and deterministic: the same content ingested via schedule,
`run --once`, MCP, or web UI yields **one** point, not four.

> **Fail-fast:** enabling a source whose credential/prerequisite is unset
> makes `run` exit 2, naming the source, the missing prerequisite, and where
> to set it (e.g. `credential env var YMAIL_APP_PASSWORD is not set
> (required by sources.yahoo)`). Nothing is ingested; the failed run is still
> audited.

### Step 6 — Put it on a schedule (and/or expose it)

Pick the surface(s) you need:

```bash
# 6a — define a recurring schedule (presets: hourly, every-N-hours,
#     daily, weekly, monthly)
digital-twins schedule add --source fs --preset daily --fire-time 03:00
digital-twins schedule list

# 6b — long-running scheduler: fires due schedules
digital-twins serve

# — or — one-shot host-cron path (no long-running process):
digital-twins run --once
# cron: 0 3 * * * digital-twins run --once

# 6c — web app: UI + /api/* REST
digital-twins web

# 6d — MCP server for agent clients (stdio or HTTP/SSE)
digital-twins serve-mcp --transport stdio            # newline-delimited JSON
digital-twins serve-mcp --transport http --port 8770 # HTTP/SSE on 127.0.0.1
```

**Check** for 6a/6b: the scheduler status endpoint reports the next fire
time; due schedules fire and their runs land in `digital-twins run-history`.
Full detail:
[`docs/scheduling.md`](docs/scheduling.md),
[`docs/references/agent-guides.md`](docs/references/agent-guides.md).

### You're done

You have a first admin account, validated endpoints, a working one-shot
ingest, and a choice of schedule / web / MCP surfaces. Everything is
auditable in the state DB (`run-history`) and the Qdrant/Neo4j collections.

## Configuration

The authoritative knob reference (every shipped knob with default, type, and
precedence) lives in
[`docs/configuration.md`](docs/configuration.md). Precedence (deterministic,
highest wins): **env (incl. `.env`) → `kb.local.yml` → `kb.yml` → built-in
defaults**. All sources are disabled by default.

## Roles & tokens

Multi-user (003): three roles gate every mutating action.

| Role | What it can do |
|---|---|
| `admin` | Every capability, including cross-user (view all history, manage accounts, manage all users' config). |
| `scheduler` | Manage schedules, trigger runs, own run history, own config + own tokens. |
| `reader` | Pure query: own run history, status. Mutating routes are denied with 403 / exit 2. |

The first row in `accounts` becomes `admin`; every later sign-up (via
`digital-twins signup --email … --password …`) is a `reader`. Each account
holds **personal tokens** (self-service via
`digital-twins token create` / `list` / `revoke`): the `DT_PERSONAL_TOKEN`
env var resolves to an account + role, and the role is checked against the R3
capability matrix before any mutating action. A `reader` is denied every
mutating tool with `code=permission_denied`. Full detail:
[`docs/multi-user.md`](docs/multi-user.md).

## MCP

A full MCP server (004) exposes the scheduler surface over stdio and
HTTP/SSE so any MCP-capable agent can manage schedules and trigger runs.
The `mcp` SDK is an optional extra
(`pip install "digital-twins-kb[mcp]"`); the server itself speaks raw JSON
and runs without it.

Six scheduler tools (`kb_schedule_list` / `create` / `update` / `delete` /
`run`, `kb_run_history`) plus four BR-10 stubs (`kb_search` / `chat` /
`ingest` / `health` → `not_implemented_yet`). Tools are role-gated and
owner-scoped by default. Agent onboarding:
[`docs/references/agent-guides.md`](docs/references/agent-guides.md).

## Scheduling

Preset cadences (`hourly`, `every-N-hours`, `daily`, `weekly`, `monthly`),
the long-running `digital-twins serve` process (ticks the scheduler loop and
fires due schedules), and the one-shot host-cron alternative. The `serve`
command is the reference deployment for scheduled ingestion; `run --once` is
the one-shot equivalent. Full detail:
[`docs/scheduling.md`](docs/scheduling.md).

## Community

Versioning rules (what counts as major / minor / patch, the deprecation
mechanism, when version bumps happen):
[`docs/semver-policy.md`](docs/semver-policy.md). The in-repo issue tracker
lives in `.github/ISSUE_TEMPLATE/` — a bug report, a feature request, and a
config-breaking-change form (required `version_impact` + `migration_note`).
File issues there; no live remote required.

## License

MIT. See [`LICENSE`](LICENSE) and
[`pyproject.toml`](pyproject.toml) (`license = { text = "MIT" }`).

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run the hermetic suite (unit + integration, in-memory Qdrant + stubs)
pytest -q

# Opt-in live tests against real services
KB_LIVE_QDRANT=... KB_LIVE_NEO4J=... pytest -m live
```
