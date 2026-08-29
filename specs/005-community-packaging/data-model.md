# Data Model: Community Packaging & Documentation

005 is a **docs/packaging** slice. It adds **no new table**, **no new column**,
**no schema migration**, and **no new config knob**. The state store stays at
`PRAGMA user_version=3` (003's v3 schema) untouched. 005's "data model" is the
*documentation* of the existing config schema, the version surface, and the
changelog contract — the three things a user upgrading or packaging the package
must be able to read machine- and human-ably.

This file captures that contract. There is nothing to migrate.

## 1. No state-store change

| Item | Status |
|---|---|
| New tables | **none** |
| New columns | **none** |
| Migration step | **none** (`SCHEMA_VERSION` stays 3; `migrations.MIGRATIONS` unchanged) |
| `PRAGMA user_version` | 3 (unchanged from 003) |

Rationale: 005 ships packaging artifacts (Docker/Compose, PyPI mechanics, docs,
license, issue templates). None of these touch the ingestion state. Constitution
VI (upgrade safety) is trivially satisfied: an in-place upgrade of the *package*
that adds only packaging files cannot lose state, and no migration runs.

## 2. The config-schema surface (documented, not changed)

The config schema is the machine-readable surface in
`digital_twins/config/knobs.py` (the `KNOBS` registry) +
`digital_twins/config/schema.py` (`DEFAULTS`, `BUILTIN_SOURCES`,
`SOURCE_DEFAULTS`, `CUSTOM_SOURCE_DEFAULTS`). 005 **documents** this surface in
`docs/configuration.md` (research R4) and makes the existing
`tests/unit/test_knob_docs.py` guard its automated proof.

**Knob inventory (the 100% that `docs/configuration.md` must cover):**

| Group | Knobs | Source of truth |
|---|---|---|
| Global | `state_dir`, `config_dir` | `KNOBS` + `DEFAULTS` |
| Endpoints | `qdrant.url`, `qdrant.api_key`, `neo4j.url`, `neo4j.user`, `neo4j.password`, `llm.endpoint`, `llm.model`, `llm.api_key` | `KNOBS` (all `default: None`; user-supplied, `init` prompts) |
| Embedding | `embedding.model` (pinned `BAAI/bge-small-en-v1.5`, 384-dim), `embedding.device` (`auto`\|`cpu`\|`cuda`) | `KNOBS` + `DEFAULTS` |
| Chunking | `chunking.max_chars` (800), `chunking.overlap` (100) | `KNOBS` + `DEFAULTS` |
| Scheduler | `scheduler.status_port` (8765; `0` disables) | `KNOBS` + `DEFAULTS` |
| Sources (per built-in) | `sources.<name>.enabled` (false), `sources.<name>.max_items` (200), `sources.<name>.timeout_s` (1500) for each of `hermes`, `pi`, `dsh`, `paperclip`, `yahoo`, `gmail`, `fs` | `KNOBS` (generated from `BUILTIN_SOURCES`) |
| Sources (credential/account) | `sources.yahoo.credential` (`YMAIL_APP_PASSWORD`), `sources.yahoo.email` (`YMAIL_EMAIL`), `sources.gmail.credential` (`GMAIL_APP_PASSWORD`), `sources.gmail.email` (`GMAIL_EMAIL`) | `KNOBS` |
| Sources (custom example) | `sources.mytool.enabled`, `sources.mytool.entrypoint`, `sources.mytool.credential` (`MYTOOL_TOKEN`), `sources.mytool.prefix` | `KNOBS` (the documented custom-source example) |

**Precedence (documented verbatim in `docs/configuration.md`):**
`env (incl. `.env`) → kb.local.yml → kb.yml → user_config (per-user, 003) →
built-in defaults`. Env wins; `KB_` prefix, `__` = nesting
(`KB_QDRANT__URL` → `qdrant.url`). The per-user `user_config` layer (003 R5) is
documented here as the highest *user* layer even though it is DB-backed, not a
file layer.

**No new knob:** the Docker/Compose env vars (research R1: `QDRANT_HOST`,
`NEO4J_HOST`, `LLM_ENDPOINT`, `EMBEDDING_ENDPOINT`, …) are **deployment-level**
env that the compose service translates into the *existing* `KB_*` knobs. They
are not config-layer knobs, so they do **not** enter `KNOBS` and do **not** need
a `docs/configuration.md` row — the guard is unaffected. This is the key
boundary: a compose override is *how a container reaches the in-network
backends*, not *a new tunable the package understands*.

## 3. The version surface (documented, not changed)

The version is single-sourced in `digital_twins/__init__.py`:

```python
__version__ = "0.3.0"
```

- Read by `digital-twins --version` / `--version-json` (001).
- Read by hatchling's `[tool.hatch.version] path = "digital_twins/__init__.py"`
  at build time (research R2), so the wheel name `digital_twins-0.3.0-py3-
  none-any.whl` always matches `__version__`. **No** `dynamic = ["version"]`
  drift between the two sources.
- 005 documents this single-source invariant in `docs/release-runbook.md`
  (step 1: bump `__version__`, never edit the wheel name by hand).

## 4. The changelog contract (documented, not changed)

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/) + SemVer
(001/002/003 precedent). 005 codifies the *contract* (research R2/R3):

- **Every release** since 0.1.0 has an entry (SC-004). The existing changelog
  already has `0.3.0`, `0.2.0`, `0.1.1`, `0.1.0` — 005's slice adds a `0.5.0`
  entry (the packaging slice is a **minor** bump: it adds docs/packaging, no
  config-breaking change, no schema change).
- **Section vocabulary:** `Added` / `Changed` / `Deprecated` / `Migrated` (the
  003/002 pattern). A **config-breaking change** must appear under `Changed` or
  `Deprecated` **with a migration note**, and that is what forces a **major**
  bump (research R3 / `docs/semver-policy.md`).
- **Semver mapping (the machine-readable rule in `docs/semver-policy.md`):**
  | Change type | Bump | Changelog section |
  |---|---|---|
  | New non-breaking knob / feature / docs | `minor` | `Added` |
  | Bug fix / typo / config-doc fix | `patch` | (as applicable) |
  | **Config-breaking** (knob removed/renamed, type/default semantics changed, env-var mapping changed) | `major` + migration note | `Changed` / `Deprecated` |
  | Deprecation (old knob still works) | `minor` | `Deprecated` |

The 004/005 slices are `minor` bumps (new docs/tools, no config-breaking
change). 005's own release is `0.5.0` (minor over 0.3.0).

## 5. Entities (packaging, not state)

| Entity | Where it lives | Notes |
|---|---|---|
| **Release** | version tag + `CHANGELOG.md` entry + `dist/*.whl` | one record per release; the wheel name encodes `__version__` |
| **Config schema doc** | `docs/configuration.md` | single source for every shipped knob; guarded by the extended `test_knob_docs.py` |
| **Compose bundle** | `docker-compose.yml` + `Dockerfile` (repo root) | the reference deployment (qdrant/neo4j/llm/embedding-model/digital-twins); no pushed image in v1 |
| **Issue/feature tracker** | `.github/ISSUE_TEMPLATE/*.yml` + `docs/semver-policy.md` | in-repo, GitHub-flavored, no live remote (c3) |
| **Release runbook** | `docs/release-runbook.md` | tag → build → TestPyPI (manual) → PyPI (manual) |
| **Agent onboarding doc** | `docs/references/agent-guides.md` | the six 004 MCP tools + the four stubbed BR-10 tools (research R6) |

## Backward compatibility

- **State:** unchanged (user_version=3). An upgrade that adds only 005's
  packaging files preserves `.kbstate/`, the account DB, and all config —
  NFR-15 is trivially met (no migration runs).
- **Config:** no knob added/removed/renamed, so no migration note is required;
  `docs/configuration.md` is *additive* documentation of the existing surface.
- **Version:** 005 ships as `0.5.0` (minor). No 0.3.0 user sees a config change.
