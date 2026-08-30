# digital-twins

Environment-portable KB ingestion: layered config, fail-fast named sources, and
deterministic dedup-safe ingest into user-supplied Qdrant + Neo4j. Built per
`specs/001-package-foundation/` (see `AGENTS.md` for sources of truth).

## 5-minute quick start

The exact install → init → validate path. Requires Python ≥ 3.11.

### 1 — Install

```bash
pip install digital-twins
```

From a checkout:

```bash
pip install -e .
```

CPU-only hosts: PyPI's default `torch` wheel is ~5 GB with CUDA bundled.
Install torch from the CPU wheel index first, then the package:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install digital-twins
```

### 2 — Initialise

```bash
digital-twins init
```

Prompts for `qdrant.url`, `neo4j.url` / `neo4j.user` / `neo4j.password`, and
`llm.endpoint` / `llm.model`. Creates the config dir with `kb.local.yml`
(every source `enabled: false`), the state dir + `state.db`, and prints a
health report. Idempotent: re-running keeps existing values and exits 0.

### 3 — Validate

```bash
digital-twins validate
```

Exit 0 when every configured endpoint is reachable and the Qdrant collection
vector size matches the embedding model. A dimension mismatch is a hard error
naming the mismatch plus the remediation ("re-embed, or point at a new
collection"), exit 1.

### 3b — Local stack (Docker)

On a Docker host, bring up the full local stack (qdrant, neo4j, llm,
embedding-model, digital-twins) and write a machine-local `kb.local.yml`:

```bash
bash scripts/bootstrap-local.sh
```

On a no-GPU host the bundled `llm` is skipped; point `KB_LLM__ENDPOINT` at an
external LLM to get the full stack. Tear down with `docker compose down`.
See `docs/configuration.md` for the no-GPU external-LLM path.

### 4 — First run (the `fs` demo source)

```bash
# Point the fs source at a directory of two .md files in kb.local.yml:
#   sources.fs: { enabled: true, extra: { dir: /tmp/kb-demo } }
digital-twins run --source fs
digital-twins run --source fs   # second run
```

Run 1 ingests `fs: 2 item(s)` and writes an audit row with a `run_id`.
Run 2 reports `fs: 0 item(s)`; the collection still holds exactly 2 points —
dedup-safe, deterministic.

Fail-fast prerequisite: enabling a source whose credential is unset makes
`run` exit 2, naming the source + missing prerequisite + where to set it.
Nothing is ingested; the failed run is still audited.

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

The first row in `accounts` becomes `admin`; every later sign-up is a
`reader`. Each account holds **personal tokens** (self-service via
`digital-twins token create`): the `DT_PERSONAL_TOKEN` env var resolves to an
account + role, and the role is checked against the R3 capability matrix
before any mutating action. A `reader` is denied every mutating tool with
`code=permission_denied`. Full detail:
[`docs/multi-user.md`](docs/multi-user.md).

## MCP

A full MCP server (004) exposes the scheduler surface over stdio and
HTTP/SSE so any MCP-capable agent can manage schedules and trigger runs.
The `mcp` SDK is an optional extra
(`pip install "digital-twins[mcp]"`); the server itself speaks raw JSON and
runs without it.

Start the server:

```bash
digital-twins serve-mcp --transport stdio            # newline-delimited JSON on stdin/stdout
digital-twins serve-mcp --transport http --port 8770 # HTTP/SSE on 127.0.0.1
```

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

Pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```
