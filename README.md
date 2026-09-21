# digital-twins

Environment-portable KB ingestion: layered config, fail-fast named sources, and
deterministic dedup-safe ingest into user-supplied Qdrant + Neo4j. Built per
`specs/001-package-foundation/` (see `AGENTS.md` for sources of truth).

## Fast path (new machine to first ingest)

**No clone / no manual steps — the remote one-liner** (the
`curl | bash` entry point; it self-bootstraps Python ≥ 3.11, creates an
isolated venv, installs from PyPI, and runs the first-run wizard):

```bash
curl -fsSL https://raw.githubusercontent.com/terrygzhou/digital-twins/main/scripts/install.sh | bash
```

> **Piping into `bash`** runs code from the network. To audit first,
> download it and read it (`bash -x scripts/install.sh` traces every command):
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/terrygzhou/digital-twins/main/scripts/install.sh -o install.sh && bash install.sh
> ```
>
> The script performs no network access except the `pip install` from PyPI.

**Already have a checkout?** Run the same installer locally instead of
piping (finds Python ≥ 3.11, creates an isolated venv, installs the package,
and runs the first-run wizard):

```bash
bash scripts/install-local.sh
```

It ends with the `digital-twins setup` wizard (backend detection + init +
admin account + health checks) and tells you the next step. Re-running is a
safe no-op on a host that is already installed. Useful options:
`--extras "mcp,local-embedding"` (pick the extras), `--cloud` (no Docker;
cloud backends), `--run-ingest` (ingest a demo source right away),
`--no-setup` (stop after the pip install). See
`bash scripts/install-local.sh --help`.

**Prefer to do it by hand?** The minimal steps the installer runs:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "digital-twins-kb[mcp]"   # base + MCP; drop [mcp] if not needed
digital-twins setup            # wizard: backend detection + init + admin + health
digital-twins run --source fs  # your first ingest
```

> **PEP 668 ("externally managed" error on macOS/Homebrew, Debian, or
> other distros that protect system Python):** the venv line above is the
> clean fix on any OS. Alternative: `pipx`, which manages its own venv and
> symlinks the CLI to `~/.local/bin`:
>
> ```bash
> # macOS:
> brew install pipx && pipx ensurepath
> # Debian/Ubuntu:
> sudo apt install pipx
> # Arch:
> sudo pacman -S pipx
> # then (on any OS):
> pipx install "digital-twins-kb[mcp]"
> ```
>
> On Debian/Ubuntu where `python3 -m venv` is unavailable (missing the
> `venv` module), install it first: `sudo apt install python3-venv`.

> **CPU-only host?** PyPI's default torch wheel is ~5 GB with CUDA bundled.
> If you plan to use in-process embedding (`[local-embedding]` extra) on a
> machine without a GPU, install the CPU wheel first:
>
> ```bash
> pip install torch --index-url https://download.pytorch.org/whl/cpu
> pip install "digital-twins-kb[local-embedding]"
> ```
>
> On a GPU host skip the CPU-wheel line and just
> `pip install "digital-twins-kb[local-embedding]"`.

> **Three layers, one `-kb` suffix** (only `digital-twins-kb` goes into
> `pip install` — the bare `digital-twins` name is PyPI-blocked as too
> similar to the existing `digital-twins-kb`):
> - `digital-twins-kb` — the **PyPI distribution** name (what you pass to
>   `pip install`; PEP 503 normalizes `-`/`_`/`.` in dist names)
> - `digital-twins` — the **CLI** on your PATH after install
> - `digital_twins` — the **Python import** name (`python -m digital_twins`
>   works too)

The `setup` wizard detects the backend: when Docker is available it offers
to start the bundled local stack (qdrant + neo4j + embedding-model, plus
the bundled LLM when a GPU is present — a host with `nvidia-smi`
installed but no usable GPU is treated as no-GPU, same as the
bootstrap script) and writes `kb.local.yml` for you; when it is not, it
prompts for the three cloud endpoints instead. In either case it creates
the state DB, the first admin account (a generated password is shown
once and written to `admin-credentials.txt` in your state dir, created
with mode 600 — delete it after your first login), runs the health
checks, and prints the next step.

Flags:

| Flag | Effect |
|------|--------|
| `--skip-services` | "I handle backends myself": never probe Docker, never prompt, never write `kb.local.yml`; only init + admin + health checks. **Takes precedence over `--cloud` and over a valid `kb.local.yml`** (a re-run where the backend is already up). |
| `--cloud` | Skip Docker detection, go straight to cloud mode (prompts for the three required endpoints). Set via env (`KB_QDRANT__URL`, `KB_NEO4J__URL`, `KB_LLM__ENDPOINT`) or answer the prompts; optional `KB_EMBEDDING__ENDPOINT`, `KB_NEO4J__USER`, `KB_NEO4J__PASSWORD`. |

Exit codes: `0` all checks pass · `1` a check failed (remediation names
the endpoint) · `3` local stack's mandatory services did not become
healthy (a half-up stack where the survivors answer the health poll still
writes `kb.local.yml` and continues — you only get 3 when the survivors
also fail) · `5` cloud endpoints could not be resolved (the remediation
names exactly which of `KB_QDRANT__URL` / `KB_NEO4J__URL` /
`KB_LLM__ENDPOINT` are empty; an env var set to an empty string is
honored as "set but empty" — it fails the gate, it does not fall through
to the prompt).

## Exposing to agents (MCP)

Once installed with the `[mcp]` extra, exposing your KB to an MCP-capable
agent is two commands + one JSON block:

```bash
digital-twins token create              # mint a personal token (shown once)
digital-twins serve-mcp --http          # long-running HTTP server on 127.0.0.1:8770
```

Agent MCP config (Claude Desktop / Cursor / DSH / Hermes — same shape):

```json
{
  "mcpServers": {
    "digital-twins": {
      "url": "http://127.0.0.1:8770/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

Stdio variant (agent spawns the server as a child process):

```json
{
  "mcpServers": {
    "digital-twins": {
      "command": "digital-twins",
      "args": ["serve-mcp", "--transport", "stdio"],
      "env": { "DT_MCP_TOKEN": "<your-token>" }
    }
  }
}
```

All 10 tools (6 scheduler + 4 KB) are available; role-gating and
owner-scoping apply per call. Full quickstart:
[`specs/004-mcp-scheduler-tools/quickstart.md`](specs/004-mcp-scheduler-tools/quickstart.md).

## Manual onboarding (reference)

The steps below are what `digital-twins setup` does for you. Use them
if you want to do it manually, or to re-do one specific step.

### Prerequisites

- **Python ≥ 3.11** (`python3 --version`)
- Either **Docker** (for the bundled local stack) **or** already-running
  Qdrant + Neo4j + an OpenAI-compatible LLM + embedding endpoint you can
  point at.
- A GPU is optional — CPU-only hosts work (see the install note above).

### Install the package

The fast path above shows the minimal case. Details:

```bash
# Create a venv (recommended: system Python on many distros is
# "externally managed" and refuses direct pip installs — PEP 668)
python3 -m venv .venv
. .venv/bin/activate

# Base install (lightweight, no torch): endpoint-based embedding
# (embedding.endpoint in kb.local.yml) and all non-embed commands work.
pip install digital-twins-kb

# Optional extras (combine as needed):
#   [mcp]             — MCP SDK for external agents
#   [local-embedding] — in-process BGE model, no endpoint needed
#   [dev]             — dev dependencies
pip install "digital-twins-kb[mcp]"
pip install "digital-twins-kb[local-embedding]"
```

From a git checkout instead of PyPI:

```bash
pip install .
```

**Check:**

```bash
digital-twins --version
# digital-twins, version 0.11.0
```

### Start the backend services (pick one)

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
the tool where they live in the init step below.

### Initialise

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

### Validate

```bash
digital-twins validate
```

**Check:** exit 0, and every configured endpoint reachable with the Qdrant
collection vector size matching the embedding model. A dimension mismatch is
a hard error naming the mismatch and the remediation ("re-embed, or point at
a new collection"), exit 1.

### Enable a source and run your first ingest

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

### Put it on a schedule (and/or expose it)

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

# 6d — MCP server for external agents (see "Exposing to agents" above)
digital-twins token create              # mint a personal token
digital-twins serve-mcp --http         # HTTP on 127.0.0.1:8770
digital-twins serve-mcp --http --port 9000
digital-twins serve-mcp                # stdio NDJSON (agent spawns it)
```

> **6b vs `run --once`:** `serve` keeps a long-running process that fires
> due schedules on their exact times; `run --once` fires whatever is due
> **now** and exits, for a host cron. Pick one, not both (double-fire).
> **6c vs `serve`:** the web app is for humans (UI + REST); `serve` is the
> scheduler.

## Architecture

```
                ┌──────────────────────────────────────────────┐
                │           digital_twins (package)            │
                │                                              │
user ──────────▶ │  cli.py  ──▶  config/  ──▶  4-layer load   │
                │   (all cmds)    schema     env→local→yml→def│
                │              │                              │
                │              ▼                              │
                │  sources/ ──▶  ingest/  ──▶  state/        │
                │  (8 named)     ids+chunks   SQLite (WAL)    │
                │                    │              │         │
                │                    ▼              ▼         │
                │              Qdrant (vector)  Neo4j (graph) │
                │                                              │
                │  scheduler/  ◀── cron (host) or serve loop │
                │  web/        ◀── UI + /api/* (REST)         │
                │  mcp/        ◀── NDJSON stdio / POST /mcp   │
                └──────────────────────────────────────────────┘
```

- **config** — 4-layer, env wins; `KB_` prefix, `__` = nesting; local
  secrets in `kb.local.yml` (gitignored).
- **sources** — fail-fast prerequisite check; `fs` needs no credential;
  others need an env var (see `docs/configuration.md`).
- **ingest** — content-addressed chunk ids; deterministic; dedup-safe.
- **state** — SQLite in `~/.digital-twins` (WAL, FK on); runs + access
  log + user config overrides.
- **scheduler / web / mcp** — three independent surfaces that all call the
  same `ingest` core (NFR-14: one point, not four).

## Configuration

Knobs live in `kb.local.yml` (or env). The full list with defaults and
types: [`digital_twins/config/knobs.py`](digital_twins/config/knobs.py).

| Knob | Default | Meaning |
|------|---------|---------|
| `qdrant.url` | (unset) | Qdrant base URL — required. |
| `neo4j.url` / `neo4j.user` / `neo4j.password` | (unset) / `neo4j` / (unset) | Neo4j — required. |
| `llm.endpoint` / `llm.model` | (unset) / (unset) | OpenAI-compatible LLM — required. |
| `embedding.endpoint` | (unset) | Optional; skips local model load when set. |
| `state_dir` | `~/.digital-twins` | Where `state.db` lives. |
| `config_dir` | `~/.config/digital-twins` | Where `kb.local.yml` lives. |
| `sources.<name>.enabled` | `false` | Enable a source. |
| `sources.<name>.extra.*` | source-specific | `dir`, `mailbox`, … |
| `sources.<name>.prerequisites` | built-in | Extra prereqs; unset → fail-fast. |
| `scheduler.presets` | 5 built-ins | Recurring schedule presets. |

Full detail + examples: [`docs/configuration.md`](docs/configuration.md).

## Multi-user & tokens

Accounts and credentials live in the state DB (`~/.digital-twins`):

- **Admins** sign in with a password (PBKDF2, `sessions`); the first
  `init` creates one; `digital-twins user add` adds more.
- **Readers** sign in and get an 8 h session token (`sessions`), or a
  personal token (`personal_tokens`, `pt_` prefix, revocable).
- **Machine-to-machine** (MCP / cron) uses the shared service token
  `DT_SERVICE_TOKEN` (env), or an account email + password via
  `digital-twins token create` (see "Exposing to agents" above).

Role model (default): `reader` → read/search/list; `scheduler` →
reader + schedule CRUD/trigger; `admin` → scheduler + user management.

| `digital-twins` sub-command | Does |
|---|---|
| `login` | sign in → session token (8 h) |
| `user add/list` | create a named user; list accounts (admin) |
| `token` | mint a personal token for the signed-in account |
| `tokens revoke` | revoke a personal token (admin) |
| `status` | who-am-I + role + live service report |
| `web` | the web app (UI + /api/*); admin-only ops check the role |

Full detail: [`docs/multi-user.md`](docs/multi-user.md).

## Scheduling

Five presets: `hourly`, `every-N-hours` (`extra: {hours: N}`), `daily`
(default 03:00), `weekly` (default Monday 04:00), `monthly` (default 1st
05:00). Three surfaces: the long-running scheduler (`serve`), the host
cron one-shot (`run --once`), and the web UI `/api/schedules`.

> **Note:** `run --once` fires every due schedule and exits — for a host
> cron entry (`0 3 * * * digital-twins run --once`), not a long-running
> process. Use `serve` when you need the scheduler resident (exact-fire
> times, missed-run catch-up, status endpoint).

Full detail: [`docs/scheduling.md`](docs/scheduling.md).

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

## Uninstall

Reverses the fast path above (venv + `pip install "digital-twins-kb[mcp]"` +
`digital-twins setup` + `bash scripts/bootstrap-local.sh`). One script,
one invocation; every step is a no-op when its target is absent, so it is
safe to re-run and safe on a host that never had anything installed:

```bash
bash scripts/uninstall-local.sh                # stop docker stack, pip uninstall
bash scripts/uninstall-local.sh --tear-down-volumes   # also remove the named
                                                      # volumes (all ingested content)
bash scripts/uninstall-local.sh --remove-data         # also delete the config
                                                      # + state dirs
bash scripts/uninstall-local.sh --force               # skip every confirmation
bash scripts/uninstall-local.sh --skip-docker         # cloud/external hosts
```

> **Note:** run this with `bash`, not `sh` — the script uses
> `set -o pipefail`, which dash does not support (same caveat as the
> bootstrap script).

> **Warning — `--tear-down-volumes` on shared infrastructure:**
> the shipped `docker-compose.yml` uses **named** volumes (`qdrant-data`,
> `neo4j-data`, `digital-twins-state`). Docker volume names are **global**
> across the whole daemon, not per compose project — if you run another
> stack (yours or a teammate's) that references the same volume names,
> `docker compose down -v` will delete **their** data too, not just the
> local stack's. Check `docker volume inspect qdrant-data` before answering
> "y" to the confirmation prompt.
>
> If your qdrant/neo4j are shared, long-lived, or bind-mounted from a host
> path you maintain yourself: **don't run `--tear-down-volumes` at all** —
> use the plain `bash scripts/uninstall-local.sh` (instances stopped, data
> kept) and let whoever owns the data decide when to remove the volumes
> manually. Note that `down -v` also does *not* delete local images
> (`qdrant:1.9.7`, `neo4j:5.18-community`, the built `digital-twins` image);
> that's a separate manual `docker rmi` step this script does not touch.

What each flag does (all off by default):

| Flag | Effect |
|------|--------|
| _(none)_ | `docker compose down` (volumes kept, so a re-bootstrap resumes) + `pip uninstall -y digital-twins-kb`. Config and state dirs are kept. |
| `--tear-down-volumes` | `docker compose down -v`: also removes the named volumes (`qdrant-data`, `neo4j-data`, `digital-twins-state`) — **all ingested content is lost**. |
| `--remove-data` | Also `rm -rf` the machine-local config dir (`KB_CONFIG_DIR`, default `~/.config/digital-twins`) and state dir (`KB_STATE_DIR`, default `~/.digital-twins`) — including `kb.local.yml`, `state.db`, and `admin-credentials.txt`. |
| `--force` | Skip every interactive `y/N` confirmation. |
| `--skip-docker` | Skip the `docker compose down` step (for hosts that use cloud/external backends, i.e. `bootstrap-local.sh --cloud`). |

Each removal is confirmed interactively unless `--force` is given; a
non-interactive invocation (EOF on stdin) is treated as "no", so nothing is
deleted without an explicit yes. A pipx-managed install is detected and
reported (run `pipx uninstall digital-twins` instead) rather than
removed by this script.

Exit codes: `0` success (or everything was already absent) · `1` a step
failed and `--force` was not given (remediation names the step and the
manual command) · `2` `--tear-down-volumes` was requested but a volume could
not be identified (nothing was removed).
