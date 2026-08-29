# Research: Community Packaging & Documentation

Phase 0 output. 005 is a **docs/packaging** slice: it ships no new ingestion
behavior, no new table, and no new config knob. It packages, licenses, documents,
and version-policies the surface that 001–004 already built. Every decision
reuses shipped 001–004 machinery (hatchling build, `digital-twins` console
script, the `KNOBS` registry, the two standing guards) and adds only the
packaging artifacts this slice owns. No locked decision is re-opened.

The six plan-stage decisions the spec asks research.md to resolve are R1–R6
below. Each names the chosen approach, the rationale, and the rejected
alternative.

---

## R1 — Compose topology: five services, pinned images, host-neutral mounts

**Decision.** Ship `docker-compose.yml` at the repo root with exactly five
services. The bundle is a *shipped reference deployment* (files in-repo, the
`digital-twins` image built from the repo via a `Dockerfile`); **no** image is
pushed to a registry in v1 (locked c1 / R1).

| Service | Image (pinned, no `:latest`) | Role |
|---|---|---|
| `qdrant` | `qdrant/qdrant:1.9.7` | vector store; collection `personal_kb` (pinned in `health.py`, no knob) |
| `neo4j` | `neo4j/neo4j:5.18-community` | graph store |
| `llm` | `lmsys/sglang:v0.4.2` | OpenAI-compatible LLM runtime (BR-11.1.6 / Q7: bundle the LLM runtime) |
| `embedding-model` | `python:3.11-slim` + model baked in | pinned `BAAI/bge-small-en-v1.5` (384-dim); a tiny HTTP server exposing the embed endpoint so the host `digital-twins` container can call it |
| `digital-twins` | `digital-twins:0.3.0` (built locally from `Dockerfile`) | runs `digital-twins serve`; the `digital-twins` process + its config + state live here |

Notes:
- The exact version tags above are **plan placeholders**: the *decision* is the
  pinned, named, `no-:latest` policy plus which five services exist and how they
  wire together. The implementer records the actually-chosen tags at build time
  in `docs/release-runbook.md` and the automated compose guard only asserts
  *well-formed + no `:latest`* (not a specific tag), so a tag bump never breaks
  the guard. This keeps the guard stable across re-pins.
- **embedding-model** needs a runtime because 001's `embedding` knob is a
  *model id* consumed by `sentence-transformers` inside the `digital-twins`
  process. For the bundle to satisfy "a user with no external embedding service
  runs end-to-end from the container," the embedding service must expose an
  endpoint the `digital-twins` container can reach. **Plan decision:** the
  `embedding-model` service runs a minimal `sentence-transformers` HTTP server
  (the package already depends on `sentence-transformers`+`torch`; the service
  just wraps the pinned model behind `POST /v1/embeddings`). `digital-twins`
  reaches it via `KB_EMBEDDING__ENDPOINT` (a new compose-only env, see R1-env).
  This is a **shipped reference** the user can swap for any OpenAI-compatible
  embedding endpoint. The host `digital-twins` code is *not* modified to add an
  "embed endpoint" knob — the embedding service is a deployment-level concern,
  not a config-layer one. The `embedding.model`/`embedding.device` knobs stay
  as 001 shipped them (the in-process model is still the default for
  non-Docker installs).

- **Volumes (host-neutral, relative paths + env only):**
  - `digital-twins` mounts its `kb.yml` (the committed defaults) read-only and
    a `state` named volume for `KB_STATE_DIR`. No `./` host path is *hardcoded*
    in the committed compose — the host-side config file path is an env var
    (`KB_LOCAL_CONFIG` override) or a relative mount the user supplies, so the
    committed file itself carries no host path (passes `test_portability.py`).
  - `qdrant` and `neo4j` use named volumes (`qdrant-data`, `neo4j-data`) — not
    host bind paths. Named volumes are host-neutral.
- **Healthchecks:** `qdrant` (`curl :6333/healthz`), `neo4j`
  (`cypher-shell ... "RETURN 1"`), `llm` (`curl :8000/v1/models`), and
  `embedding-model` (`curl :8080/v1/models` or a readiness probe). The
  `digital-twins` service sets `depends_on: {qdrant: {condition: service_healthy},
  neo4j: {...}, llm: {...}, embedding-model: {...}}` so `init`/`serve` start
  only after the backends report healthy.
- **How `digital-twins health` passes from inside:** there is **no standalone
  `health` CLI command** in 001–004 (health checks run inside `init` and
  `validate`). The bundle's health proof is therefore `digital-twins validate`
  run *inside* the `digital-twins` service (`docker compose exec digital-twins
  digital-twins validate`), which exits 0 only when Qdrant + Neo4j + LLM are all
  reachable from within the network. The spec's "`digital-twins health` passes
  from inside" is read as "`validate` (the health-report command) exits 0 from
  inside the container." This is recorded as a **new ambiguity** (see report
  §b) — the spec repeatedly says "health" but the shipped command is `validate`.
  The quickstart uses `validate`; the README documents the equivalence.

**Env-var override surface (external-endpoint opt-in, BR-11.1.6):** the compose
file exposes these on the `digital-twins` service (all default to the bundled
in-network endpoints; set any to point at an external backend without rebuilding
the image):

| Env var (compose) | Maps to config knob | Bundled default (in-network) |
|---|---|---|
| `QDRANT_HOST` | `qdrant.url` | `http://qdrant:6333` |
| `NEO4J_HOST` | `neo4j.url` | `bolt://neo4j:7687` |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j.user` / `neo4j.password` | `neo4j` / `<compose-pinned>` |
| `LLM_ENDPOINT` | `llm.endpoint` | `http://llm:8000/v1` |
| `EMBEDDING_ENDPOINT` | (compose-only; embed service base) | `http://embedding-model:8080/v1` |
| `SGLANG_ENDPOINT` (alias) | `llm.endpoint` | — (BR-11.1.6's example name) |

These are **compose-level** env that the `digital-twins` service translates into
the package's `KB_*` knobs (via `environment:` + a thin entrypoint that sets
`KB_QDRANT__URL=${QDRANT_HOST:-http://qdrant:6333}` etc.). They are *not* new
config-layer knobs — `test_knob_docs.py` is unaffected. The README documents both
modes (bundled-only, and bundled-with-external-endpoints) per BR-11.1.6.

**Alternatives rejected:**
- A single "all-in-one" image (rejected: BR-11.1.6 names the compose file as the
  artifact and requires five logically distinct components; a fat image would
  couple the embedding model's torch rebuild to a `digital-twins` code change).
- Pushing a pre-built image to a registry in v1 (rejected: c1 locks v1 =
  user-builds; no registry target).
- Hardcoding host bind mounts (rejected: NFR-13 / c6 — the committed compose
  must pass `test_portability.py`; named volumes + env are the host-neutral
  form).

---

## R2 — PyPI build mechanics: hatchling wheel, `pip show` reports MIT

**Decision.** Build with `python -m build` (which invokes the hatchling backend
already declared in `pyproject.toml`). The automated in-suite proof (SC-006 /
US6) is: in a throwaway venv, `python -m build` produces a wheel; install that
wheel; `pip show digital-twins` reports `License: MIT`; `docs/release-runbook.md`
exists. The live TestPyPI round-trip is a **manual** runbook step (needs network
+ credentials) and is *not* an in-suite test (c5).

- **Build command:** `python -m build --wheel` (builds only the wheel; the
  sdist is not required for PyPI in v1 but the runbook shows the full
  `python -m build` for completeness). Artifacts land in `dist/`
  (`digital_twins-<version>-py3-none-any.whl`). The version comes from the single
  source `digital_twins/__init__.py` (`__version__ = "0.3.0"`), read by
  `[tool.hatch.version] path = "digital_twins/__init__.py"` — **no** `dynamic`
  version drift.
- **Wheel naming:** hatchling emits `digital_twins-0.3.0-py3-none-any.whl`
  (underscore in the normalized name `digital_twins`, hyphen-free ABI tag). The
  runbook documents this exact pattern so a second maintainer recognizes a
  correct artifact.
- **How `pip show` reports MIT:** `pyproject.toml` already declares
  `license = { text = "MIT" }` **and** a `LICENSE` file (MIT text, "Copyright (c)
  2026 Terry Zhou") already exists at the repo root. So `pip show` / PyPI will
  report `License: MIT` with **no change** to `pyproject.toml`. The 005 slice
  *verifies* this (the automated test asserts `pip show`'s License field equals
  `MIT` and that `LICENSE` is at the root) rather than *adds* it. The plan notes
  that `license = { text = "MIT" }` is the PEP 621 *legacy* table form; the
  modern form is `license = "MIT"` (SPDX). **Decision:** keep `{ text = "MIT" }`
  in v1 (it is what 001 shipped, it already satisfies `pip show`, and switching
  to the SPDX string is a non-breaking cleanup deferred to a follow-up so this
  slice does not touch the manifest). The runbook notes the SPDX upgrade as a
  future polish item.
- **Release-runbook steps** (documented in `docs/release-runbook.md`):
  1. Bump `digital_twins/__init__.py` `__version__` to the target version.
  2. Add a `CHANGELOG.md` entry (Keep a Changelog sections; see R3).
  3. `python -m build` in a clean venv → `dist/`.
  4. *(manual)* `twine upload --repository-url https://test.pypi.org/legacy/
     dist/*` with a TestPyPI token; `pip install --index-url
     https://test.pypi.org/simple/ digital-twins` round-trip.
  5. *(manual)* `twine upload dist/*` to PyPI with a PyPI API token.
  6. Tag `v<version>` in the repo.
  TestPyPI/PyPI publish is explicitly **manual + credential-gated** (out of the
  automated suite).

**Alternatives rejected:**
- `hatch build` directly (rejected: `python -m build` is the PEP 517 standard
  entry and is what the spec's SC-006 names; hatchling is already the backend,
  so no extra tooling).
- `pip install .` as the build check (rejected: that installs in-place from the
  source tree and does not prove a *distributable* artifact builds; the spec
  wants a wheel).

---

## R3 — In-repo tracker layout: `.github/ISSUE_TEMPLATE` + semver policy

**Decision.** The "machine-readable issue/feature tracker" (c3, BR-11.6.3) is
fully in-repo, GitHub-flavored, with **no live GitHub dependency** (this repo has
no remote). Exact files:

```
.github/
└── ISSUE_TEMPLATE/
    ├── config.yml                 # blank-issue_enabled: false; contact links off
    ├── bug_report.yml             # structured bug form
    ├── feature_request.yml        # structured feature form
    └── config-breaking-change.yml # structured form for a config-breaking change
docs/
└── semver-policy.md               # the versioning policy (what a bump means)
```

- **`config.yml`** disables blank issues and lists the three templates; it is
  the "machine-readable" entry point (GitHub reads this to drive the UI; offline
  it is the human-readable index of the tracker surface).
- **The machine-readable convention** is the **YAML structure of the templates
  themselves** plus `docs/semver-policy.md`: every template emits a fixed set of
  keys (`area`, `component`, `severity` / `type` / `version_impact`) so a script
  (or a human) can parse an issue's shape without a live GitHub API. The
  `config-breaking-change.yml` template has a **required** `version_impact` field
  with a controlled vocabulary: `major` | `minor` | `patch`, and a required
  `migration_note` field. That is the machine-readable link between a
  config-breaking issue and a major bump.
- **How a config-breaking change maps to a major version bump:**
  1. A config-breaking change is *proposed* via `config-breaking-change.yml`
     (the only template that carries `version_impact: major` + a required
     `migration_note`).
  2. `docs/semver-policy.md` states the rule verbatim: *a breaking config change
     (removing/renaming a knob, changing a knob's type/default semantics, or
     changing the env-var mapping) requires a **major** bump and a migration
     note in the release notes.* Non-breaking config additions are `minor`;
     config docs/typos are `patch`.
  3. At release, the runbook (R2 step 2) cross-checks: if the changelog's
     `Changed`/`Deprecated` section contains a config-breaking entry, the version
     bump *must* be `major` and the release notes *must* carry the migration note
     (this is the "at least one real release demonstrates compliance" check,
     SC-004 / US3-AS2).

**Alternatives rejected:**
- A live GitHub remote + GitHub Issues (rejected: c3 — the repo has no remote;
  the tracker must work offline).
- A bare `ISSUE_TEMPLATE.md` single file (rejected: c3 wants *three* templates —
  bug + feature + config-breaking-change — so the config-breaking-change flow
  has its own machine-readable shape).

---

## R4 — Config-reference generation: `docs/configuration.md` + extended `test_knob_docs.py`

**Decision.** `docs/configuration.md` is the single authoritative, human-readable
config reference: **100% of shipped knobs**, each with default, type, env-var
mapping, group, and precedence. The automated proof is the existing
`tests/unit/test_knob_docs.py` guard **extended** to assert full coverage of
`docs/configuration.md` (SC-003 / US1-AS2 / US4-AS2 / c2 / c6).

**How the doc enumerates 100% of the knobs:**
- The knob surface is *already* machine-readable in
  `digital_twins/config/knobs.py` (`KNOBS` dict: every knob with `type`,
  `default`, `env`, `group`) and `config/schema.py` (`DEFAULTS`,
  `BUILTIN_SOURCES`, `SOURCE_DEFAULTS`, `CUSTOM_SOURCE_DEFAULTS`). `docs/
  configuration.md` is generated **by hand from the registry** (it is a docs
  artifact, not a build output — the repo ships it as a committed file, like
  `config.example.yml`). The structure mirrors the six `GROUP_*` sections in
  `knobs.py`: Global, Endpoints, Embedding, Chunking, Scheduler, Sources — each a
  table of `knob | type | default | env var | notes`.
- **Extending the guard.** The current `test_knob_docs.py` already enforces
  `KNOBS ↔ config.example.yml ↔ .env.example` lock-step (the 001 SC-002 knob-doc
  sync). 005 **adds** a new test class that parses `docs/configuration.md` and
  asserts:
  1. **Every** `KNOBS` key appears in `docs/configuration.md` (100% coverage —
     a new knob with no doc row fails the test).
  2. **No** doc row names a knob that is not in `KNOBS` (no phantom knob).
  3. For each knob, the doc's documented `env` var and `default` match the
     registry (a drift between registry and doc fails the test).
  The parser is a lightweight Markdown table reader (the doc uses a fixed
  table-per-group format, so parsing is a simple `| key | type | default | env |
  notes |` line scan) — the same "parse the committed artifact" philosophy as the
  existing `config.example.yml` parser. The guard therefore becomes the *single
  automated proof* that the reference is 100% complete and in sync.
- **Why hand-written, not a generator:** a generator would need to be run on
  every knob change and committed (two artifacts to keep in sync: the generator
  *and* its output). The repo's existing pattern (`config.example.yml`,
  `README.md`) is hand-maintained + guard-checked. 005 follows that pattern:
  hand-maintain the doc, and let the *extended guard* catch drift. This is
  consistent with constitution IV (config-first: every knob is documented) and
  with the 001 precedent.

**Alternatives rejected:**
- Auto-generate `docs/configuration.md` into the tree (rejected: adds a build
  step + a generator to maintain; the guard-check approach is the established
  001/002 pattern and keeps the doc reviewable in PRs).
- Point the README at `config.example.yml` instead of a dedicated doc (rejected:
  c2 locks the reference in `docs/configuration.md`; the README stays
  5-minute-sized and *links* to it).

---

## R5 — README 5-minute quick start: the exact command sequence

**Decision.** The README's 5-minute quick start is the host-neutral,
clean-machine path `install → init → first run`, referencing `docs/` for depth.
The exact sequence (it appears verbatim in `quickstart.md` and in the README):

```bash
# 0) a fresh state dir (the state_dir knob; never a hardcoded host path)
export KB_STATE_DIR="$HOME/.digital-twins"

# 1) install (PyPI once published; from source until the first publish)
pip install digital-twins          # or: pip install .  (source tree, hatchling)

# 2) init: prompts for qdrant.url / neo4j.url / neo4j.user / neo4j.password /
#    llm.endpoint, creates the state dir + state.db, writes a starter
#    kb.local.yml (every source disabled), runs the health report.
#    Non-interactive (CI / host-cron): set the INIT_ADMIN_* + endpoint env vars.
digital-twins init

# 3) validate: exits 0 only when Qdrant + Neo4j + LLM are all reachable
digital-twins validate

# 4) first run: enable one source with credentials in kb.local.yml, then
#    ingest once. The fs source (a directory of .md files) is the no-credential
#    demo source.
#   kb.local.yml:
#     sources:
#       fs:
#         enabled: true
#         extra: { dir: "<a directory of .md files>" }
digital-twins run --source fs
digital-twins run --source fs      # second run -> 0 items (dedup invariant, NFR-1)
```

- **Host-neutral:** every path is a `$KB_STATE_DIR` / `<placeholder>` — no
  `~/.` literal, no `python3.N` pin, no username (passes `test_portability.py`).
  The "first run" is the `fs` demo source because it needs **no external
  credential** (it reads a local directory), making it the only source a clean
  machine can run without secrets. (Hermes/pi/DSH/mail all need a host store or
  an app password — they are the *later* sources, documented, not the 5-minute
  path.)
- **Reference the docs:** the README's quick start ends with pointers to
  `docs/configuration.md` (full knobs), `docs/scheduling.md` (002), `docs/
  multi-user.md` (003), `docs/references/agent-guides.md` (004 MCP tools), and
  `docs/release-runbook.md` (how maintainers publish). The README itself stays
  5-minute-sized (c2 / US1): it does *not* inline the full config reference.
- **`digital-twins health` note:** as R1 found, there is no standalone `health`
  command; step 3 uses `validate` (the health-report command). The README's
  quick start says "validate (health check)" so the equivalence is explicit.

**Alternatives rejected:**
- Make the 5-minute path the Docker bundle (rejected: NFR-12's 30-minute and
  SC-001's 5-minute targets are the *pip* path; the Docker bundle is the
  secondary convenience channel and is a manual `docker compose up`).
- Include a credential source (hermes/yahoo) in the 5-minute path (rejected: it
  would require the user to have that host store / app password, which breaks
  the "clean machine, no host context" premise).

---

## R6 — 004 doc dependency: `docs/references/agent-guides.md` documents the six MCP tools

**Decision.** 005 sequences **after** 004 (locked A4 / BR-11.5.2), so
`docs/references/agent-guides.md` can reference the **real** six 004 tool
names + schemas (from `specs/004-mcp-scheduler-tools/` ruling R2 / R4) rather
than placeholders. The doc is the canonical "any MCP agent" onboarding page and
gains:

- **The six scheduler tools** (004), each with name, args (mirroring 003
  `contracts/scheduler.md` field names: `schedule_id`, `source`, `preset`,
  `param`, `fire_time`, `acl`), return shape, and **role requirement** (004 R4 /
  003 R3):
  | Tool | Capability required (003 R3) |
  |---|---|
  | `kb_schedule_list` | `query_status` (own) |
  | `kb_schedule_create` / `kb_schedule_update` / `kb_schedule_delete` | `schedule_crud` |
  | `kb_schedule_run` | `trigger_run` |
  | `kb_run_history` | `view_own_history` (own) / `view_all_history` (all, admin) |
  A `reader` may `kb_schedule_list` + `kb_run_history` (own) but is denied every
  mutating tool with `code=permission_denied` naming the missing capability.
- **The four stubbed BR-10 tools** (004 ruling R10): `kb_search`, `kb_chat`,
  `kb_ingest`, `kb_health` — documented as *registered-but-stubbed* in v1
  (each returns `code=not_implemented_yet` naming the follow-up slice), so a
  fresh client sees the complete registry and knows which are real vs.
  follow-up.
- **Onboarding line** (BR-11.5.2): an agent with a valid token + transport URL
  gets the full tool list; the token is the only host-specific thing (no host
  paths, no usernames — NFR-13).

**Sequencing guarantee:** 004's spec is `specify+clarify complete (plan/tasks
pending)`; 005's plan assumes 004 lands its tool registry before 005 writes the
doc. The doc references 004's *contract* (tool names + field names are stable
per 004 ruling R2), not 004's internal implementation, so the doc is correct as
long as 004 keeps its stable agent-facing contract. If 004 is not yet implemented
when 005's tasks run, the doc is written against the contract and verified
against the real tool registry at 005's review time (a 005 task: "agent-guides
tool names match 004's registry").

**Alternatives rejected:**
- Stub the whole agent-guides section until 004 lands (rejected: 005's FR / US
  names the doc as a 005 deliverable; the spec wants it shipped in 005).
- Document a 7th/8th tool (rejected: 004's scope is exactly six scheduler tools
  + four stubbed BR-10 tools; 005 documents what 004 ships, no more).

---

## Constitution gate (compliance check)

| Principle | 005 status |
|---|---|
| I. Portability & Environment Neutrality (CRITICAL) | **Enforced.** Every 005 artifact (README, docs/*, docker-compose.yml, Dockerfile, .github/*, pyproject) is added to `test_portability.py`'s `SHIPPED` list and must pass (no host paths, no `python3.N`, no literal usernames, no `~/.` absolute). The compose uses named volumes + env, not host binds. |
| II. Deterministic, Idempotent Ingestion | **N/A (no pipeline change).** 005 ships no ingestion code; the dedup invariant is untouched. |
| III. Test-First | **Enforced.** The extended `test_knob_docs.py` (100% doc coverage) and the extended `test_portability.py` (compose + new docs) are written **red first**, then the docs/compose are written to make them pass. |
| IV. Config-First, Fail-Fast | **Enforced.** `docs/configuration.md` documents 100% of the `KNOBS` registry (the guard proves it). No new knob is introduced by 005 (the compose env vars are deployment-level, not config-layer knobs). |
| V. Auditability & Observability | **N/A** (no run path added). |
| VI. Upgrade Safety & Versioning | **Enforced.** `docs/semver-policy.md` codifies the bump rules; the changelog-driven release (R2/R3) is the mechanism; a config-breaking change → major + migration note. |

**Complexity Tracking (required deviations):** none. 005 adds no code, no
dependency, no knob; it adds packaging artifacts + docs + two extended guards.
No simpler alternative was rejected.

---

## NEEDS-CLARIFICATION resolutions (all resolved, none open)

The spec/clarify stage had no unresolved `NEEDS-CLARIFICATION` markers after
D-1 (LLM bundling) was resolved. The remaining plan-stage unknowns — the six
decisions above — are now resolved in R1–R6. No new owner decision is required;
every choice is within the locked rulings (Q4 MIT, Q7 bundle, c1–c6, A1–A5).

## New ambiguities found during planning (flag for the report)

1. **"`digital-twins health`" vs. the shipped `validate` command.** The spec
   (US5/SC-005, quickstart, BR-11.1.3) repeatedly says "health passes from
   inside," but 001–004 ship **no standalone `health` command** — health checks
   run inside `init` and `validate`. **Resolution (plan decision, not re-opening
   the spec):** treat "`digital-twins health`" as "`digital-twins validate`"
   (the health-report command). The quickstart and README use `validate` and
   state the equivalence. This does *not* change the spec's intent (a health
   proof from inside the container); it just names the real command. If the
   owner wants a literal `health` subcommand, that is a **006/follow-up** CLI
   addition, not a 005 packaging change.
2. **`license = { text = "MIT" }` is the PEP 621 legacy form.** The modern form
   is `license = "MIT"` (SPDX). 005 keeps the legacy form (it already satisfies
   `pip show`) and defers the SPDX cleanup to a follow-up so this slice does not
   touch the manifest. Flagged as a minor, non-blocking polish item.
3. **The `embedding-model` service shape.** BR-11.1.6 says "bundle the embedding
   model" but 001's embedding knob is an in-process model id (not an endpoint).
   For the container to satisfy "no external embedding service," the bundle adds
   a thin `sentence-transformers` HTTP service (R1). This is a *deployment*
   artifact, not a config-layer knob; the host `digital-twins` code is unchanged.
   Flagged so the implementer knows the embedding service is a compose-level
   concern.
