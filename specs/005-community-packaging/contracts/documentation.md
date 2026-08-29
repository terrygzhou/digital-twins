# Contract: 005 Deliverable Artifacts (Community Packaging & Documentation)

This is the **deliverable contract** for 005: the exact set of files the slice
ships, the shape each must have, and the automated check that proves each. 005
ships **no Python code** and **no new knob**; it ships packaging artifacts,
docs, and two *extended* standing guards. The contract is the acceptance
surface: a file is "done" when its shape below is met and its check passes.

All artifacts are **host-neutral** (constitution I, CRITICAL): no host path, no
`python3.N` pin, no literal username, no `~/.` absolute path. Every artifact is
added to `test_portability.py`'s `SHIPPED` list and must pass.

---

## C-1 — `README.md` (5-minute quick start)

**Shape:** one-paragraph what-it-is → 5-minute quick start → link to
`docs/configuration.md` → "add a new source" how-to (BR-11.6.1). Stays
5-minute-sized; the full config reference lives in `docs/configuration.md`, not
here (c2 / US1-AS2).

**Required sections (in order):**
1. **What it is** — one paragraph (portable KB ingestion: layered config,
   fail-fast named sources, deterministic dedup-safe ingest into user-supplied
   Qdrant/Neo4j).
2. **5-minute quick start** — the exact `install → init → validate → first run`
   sequence from research R5 (host-neutral; `KB_STATE_DIR` placeholder; the `fs`
   demo source as the no-credential first run).
3. **Configuration** — a pointer to `docs/configuration.md` (the 100% knob
   reference) + a one-line precedence note. *Not* the full knob table.
4. **Add a new source** — the custom-source how-to (the 001 `mytool` example;
   `entrypoint`/`credential`/`prefix`).
5. **Docs index** — links to `docs/configuration.md`, `docs/scheduling.md`
   (002), `docs/multi-user.md` (003), `docs/references/agent-guides.md` (004),
   `docs/release-runbook.md`, `docs/semver-policy.md`.
6. **Docker** — the two-mode note (bundled-only / bundled-with-external-
   endpoints) + the `docker compose up` command (manual, needs a Docker host).

**Automated check:** `test_portability.py` scans `README.md` (host-neutral).
The 5-minute path is the **manual** proof (SC-001): a clean machine follows the
README verbatim and reaches a passing first run in <5 minutes.

---

## C-2 — `docs/configuration.md` (100% knob coverage)

**Shape:** the single authoritative, human-readable config reference. Six
section tables mirroring the `GROUP_*` constants in `config/knobs.py`:
Global, Endpoints, Embedding, Chunking, Scheduler, Sources. Each table row:
`knob | type | default | env var | notes`. A precedence section (the four file
layers + the per-user `user_config` layer) and a "how env vars map" section
(`KB_` prefix, `__` = nesting, examples).

**Invariants:**
- **100% coverage:** every key in the `KNOBS` registry (Global/Endpoints/
  Embedding/Chunking/Scheduler + per-built-in `sources.<name>.*` + the
  yahoo/gmail credential/email + the `mytool` custom example) has a row.
- **No phantom knob:** no row names a knob absent from `KNOBS`.
- **In sync:** each row's documented `env` var and `default` match the registry.

**Automated check (the SC-003 proof):** `tests/unit/test_knob_docs.py`
**extended** with a `TestKnobsDocumentedInConfigurationDoc` class that parses
`docs/configuration.md` and asserts the three invariants above (research R4).
Red test written first (constitution III), then the doc written to make it pass.

**Edge case (deprecated knob, US4-AS3):** the doc documents the *deprecation
mechanism* — a one-run warning naming the replacement — and notes the required
test fixture (the fixture is in `tests/`, not in the doc; the doc describes the
behavior). 005 does **not** deprecate any real knob; it documents the mechanism
as a required deliverable (c6).

---

## C-3 — `LICENSE` (MIT) + `pyproject.toml` declaration

**Shape:** MIT license text at the repo root, copyright holder = project owner
(already present: "Copyright (c) 2026 Terry Zhou"). `pyproject.toml` declares
`license = { text = "MIT" }` (already present).

**Invariants:**
- `LICENSE` at root is detectable as MIT by a license detector (US2-AS2).
- `pip show digital-twins` (from the built wheel) reports `License: MIT`
  (US2-AS1 / SC-002).

**Automated check:** the build test (C-8) asserts `pip show`'s License field ==
`MIT` and that `LICENSE` exists at root. **No change** to `pyproject.toml` is
required (001 already shipped the declaration + file); 005 *verifies* it.

**Note (flagged, research §b2):** `license = { text = "MIT" }` is the PEP 621
legacy form; the SPDX string form `license = "MIT"` is the modern form. 005
keeps the legacy form (it already satisfies `pip show`) and defers the SPDX
cleanup to a follow-up so this slice does not touch the manifest.

---

## C-4 — `docs/semver-policy.md` (versioning policy)

**Shape:** the machine- and human-readable versioning rules (BR-11.6.3).
Sections:
1. **SemVer baseline** — `major.minor.patch` from `__version__` (single source
   in `digital_twins/__init__.py`).
2. **What each bump means** — the table from data-model §4 (new knob/feature =
   minor; bug/doc fix = patch; **config-breaking = major + migration note**;
   deprecation = minor).
3. **Config-breaking definition** — the precise trigger set: removing/renaming
   a knob, changing a knob's declared type, changing default semantics, or
   changing the env-var mapping. Anything in that set is `major`.
4. **The mapping to the tracker** — a config-breaking change is proposed via
   `.github/ISSUE_TEMPLATE/config-breaking-change.yml` (required
   `version_impact: major` + required `migration_note`); at release the runbook
   cross-checks that the changelog's breaking entry forces the major bump.
5. **Changelog linkage** — every release since 0.1.0 has a `CHANGELOG.md` entry
   (SC-004); a breaking release's notes carry the migration note.

**Automated check:** existence + host-neutrality (portability guard). The
"at least one real release demonstrates compliance" (SC-004 / US3-AS2) is the
manual release-time cross-check (the runbook step).

---

## C-5 — `.github/ISSUE_TEMPLATE/` (in-repo tracker)

**Shape (research R3):** four files, GitHub-flavored, no live remote:

```
.github/ISSUE_TEMPLATE/
├── config.yml                   # blank-issue_enabled: false; lists the 3 forms
├── bug_report.yml               # structured bug form
├── feature_request.yml          # structured feature form
└── config-breaking-change.yml   # structured form; required version_impact + migration_note
```

**Invariants:**
- `config.yml` disables blank issues and lists the three forms (the
  machine-readable entry point; offline it is the tracker index).
- `config-breaking-change.yml` carries a **required** `version_impact` field
  (vocabulary `major`|`minor`|`patch`) and a **required** `migration_note`
  field — the machine-readable link to the major-bump rule (C-4).
- No template references a host path / username / interpreter pin.

**Automated check:** existence + host-neutrality (portability guard, extended to
`.github/ISSUE_TEMPLATE/`). The "machine-readable" property is the fixed key set
each form emits (`area`, `component`, `severity`/`type`/`version_impact`,
`migration_note`).

---

## C-6 — `docker-compose.yml` + `Dockerfile` (reference deployment)

**Shape (research R1):** `docker-compose.yml` at the repo root with **five**
services, all pinned (no `:latest`):

| Service | Image (placeholder tag; pinned, no `:latest`) |
|---|---|
| `qdrant` | `qdrant/qdrant:1.9.7` |
| `neo4j` | `neo4j/neo4j:5.18-community` |
| `llm` | `lmsys/sglang:v0.4.2` |
| `embedding-model` | `python:3.11-slim` (model baked in; pinned `BAAI/bge-small-en-v1.5`) |
| `digital-twins` | `digital-twins:0.3.0` (built locally from `Dockerfile`) |

**Invariants (the SC-005 automated proof):**
- **Well-formed YAML** (parses).
- **Required services present:** `qdrant`, `neo4j`, `digital-twins`, `llm`,
  `embedding-model` (the full list, per the updated SC-005).
- **Pinned images:** every `image:` has an explicit version tag; no `:latest`.
- **Config-in mounts:** the `digital-twins` service mounts a `kb.yml` / config
  read-only and a state volume; customization is config-in, image-out (US5-AS2).
- **Host-neutral:** no host path, `python3.N` pin, literal username, or `~/.`
  absolute path in the committed file (portability guard extended to
  `docker-compose.yml`). Named volumes + env vars, not host binds.
- **`depends_on` with `condition: service_healthy`** on the backend services;
  each backend has a `healthcheck`.

**Automated check (SC-005 automated):** an in-suite test parses
`docker-compose.yml` and asserts the invariants above. The live
`docker compose up` → healthy stack → `digital-twins validate` (in-container)
exits 0 is the **manual** proof (needs a Docker host; US5-AS1/AS2, SC-005 manual).

> The exact image tags in the table are **plan placeholders** (research R1):
> the guard asserts *pinned + no `:latest`*, not a specific tag, so a re-pin
> never breaks the guard. The implementer records the actually-chosen tags in
> `docs/release-runbook.md`.

**`Dockerfile`:** builds the `digital-twins` image from the repo (installs the
package via hatchling, copies the package). It is host-neutral (no `python3.N`
*literal pin* in a way that leaks a host assumption — it uses the package's
declared `requires-python >= 3.11` range; a `python:3.11-slim` *base image*
reference is acceptable because it is the container base, not a host pin — see
the portability-pattern note below).

---

## C-7 — `docs/release-runbook.md` (PyPI publication)

**Shape (research R2):** the repeatable release procedure any maintainer follows
cold (US6-AS3). Sections:
1. **Pre-release:** bump `__version__` (single source), add the `CHANGELOG.md`
   entry (Keep a Changelog sections), confirm `test_portability.py` +
   `test_knob_docs.py` green.
2. **Build:** `python -m build` in a clean venv → `dist/digital_twins-<v>-py3-
   none-any.whl`.
3. **Automated check (in-suite):** wheel builds + `pip show` reports MIT
   (SC-006 automated).
4. **TestPyPI (manual, credential-gated):** `twine upload --repository-url
   https://test.pypi.org/legacy/ dist/*` with a token; `pip install --index-url
   https://test.pypi.org/simple/ digital-twins` round-trip.
5. **PyPI (manual, credential-gated):** `twine upload dist/*` with a PyPI token.
6. **Tag:** `git tag v<version>`.
7. **Record the actual image tags** used in the Docker bundle (research R1
   placeholder resolution).

**Invariants:** `docs/release-runbook.md` exists (SC-006 automated); it names
the TestPyPI/PyPI steps as **manual** (needs network + credentials); it does not
embed host paths / credentials / a `python3.N` pin.

**Automated check:** existence + host-neutrality (portability guard, extended to
`docs/release-runbook.md`). The build+`pip show` is the automated proof
(SC-006); the TestPyPI round-trip is manual.

---

## C-8 — `docs/references/agent-guides.md` (004 tools)

**Shape (research R6):** the canonical "any MCP agent" onboarding page
(BR-11.5.2). Sections:
1. **Onboarding line** — an agent with a valid token + transport URL gets the
   full tool set; the token is the only host-specific thing (NFR-13).
2. **The six scheduler tools (004)** — each with name, args (mirroring 003
   `contracts/scheduler.md` field names: `schedule_id`, `source`, `preset`,
   `param`, `fire_time`, `acl`), return shape, and the **role requirement**
   (004 R4 / 003 R3). A `reader` may `kb_schedule_list` + `kb_run_history`
   (own) but is denied every mutating tool with `code=permission_denied`.
3. **The four stubbed BR-10 tools (004 R10)** — `kb_search`, `kb_chat`,
   `kb_ingest`, `kb_health`: registered-but-stubbed in v1, each returns
   `code=not_implemented_yet` naming the follow-up slice. Documented so a fresh
   client sees the complete registry and knows real vs. follow-up.
4. **Transport** — stdio + HTTP/SSE (004 FR-5); a community user points any MCP
   client (Claude Desktop, Cursor, pi, DSH, Hermes) at their local `kb-mcp`
   with a token.

**Invariants:** tool names + field names match 004's stable contract (004
ruling R2); host-neutral; the doc is correct against 004's *contract* (verified
against the real 004 tool registry at 005's review — a 005 task).

**Automated check:** existence + host-neutrality (portability guard, extended to
`docs/references/agent-guides.md`). The "tool names match 004's registry" is a
005 review-time cross-check (005 sequences after 004).

---

## C-9 — Extended standing guards (the automated proofs)

005 extends the two 001 standing guards (constitution III: red test first):

1. **`tests/integration/test_portability.py`** — add to the `SHIPPED` list:
   `README.md` (already present), `CHANGELOG.md` (already present),
   `config.example.yml` (already present), `.env.example` (already present),
   `LICENSE` (already present), **plus the new 005 files:**
   `docs/configuration.md`, `docs/semver-policy.md`, `docs/release-runbook.md`,
   `docs/references/agent-guides.md`, `docker-compose.yml`, `Dockerfile`,
   `.github/ISSUE_TEMPLATE/` (all files). The existing three patterns
   (`HOST_PATTERNS`, `INTERPRETER_PIN`, `LITERAL_USERNAMES`) are re-run over
   them. **No new pattern** is needed — the existing host-neutrality patterns
   already cover 005's artifacts.
2. **`tests/unit/test_knob_docs.py`** — add `TestKnobsDocumentedInConfigurationDoc`
   (research R4): parses `docs/configuration.md`, asserts 100% coverage + no
   phantom knob + env/default in sync with `KNOBS`.

**Portability-pattern note (C-6 / Dockerfile):** a `python:3.11-slim` *container
base image* line in the `Dockerfile` is **not** a host pin — it is the container
base, and it matches the package's declared `requires-python >= 3.11` range
(BR-11.2.6). The `INTERPRETER_PIN` pattern (`python3\.\d+`) matches `python3.11`
*shorthand*; the Dockerfile uses the `python:3.11-slim` *image reference* form
(`python:` prefix), which the pattern does **not** match (it matches `python3.N`,
not `python:N`). If the implementer's Dockerfile would trip the pattern, they
use the image-reference form or add a narrowly-scoped allow-list entry to the
guard with a comment — never a silent pattern edit. This is recorded so the red
test is written knowing the exact boundary.

---

## Deliverable file map (the full 005 surface)

```
NEW files (005):
  docs/configuration.md            # C-2 (100% knob reference)
  docs/semver-policy.md            # C-4
  docs/release-runbook.md          # C-7
  docs/references/agent-guides.md  # C-8
  docker-compose.yml               # C-6
  Dockerfile                       # C-6
  .github/ISSUE_TEMPLATE/config.yml                  # C-5
  .github/ISSUE_TEMPLATE/bug_report.yml             # C-5
  .github/ISSUE_TEMPLATE/feature_request.yml        # C-5
  .github/ISSUE_TEMPLATE/config-breaking-change.yml # C-5
MODIFIED files (005):
  README.md                        # C-1 (5-min quick start + docs index + Docker)
  CHANGELOG.md                     # +0.5.0 entry (the 005 slice)
  pyproject.toml                   # NO change needed (LICENSE + license already
                                   #   declared); C-3 verifies, C-8 notes the
                                   #   deferred SPDX cleanup
  tests/integration/test_portability.py  # C-9 (extend SHIPPED)
  tests/unit/test_knob_docs.py               # C-9 (extend: configuration.md coverage)
ALREADY PRESENT, VERIFIED (no change):
  LICENSE                          # C-3 (MIT, project owner)
```

## Invariants (all 005 deliverables)

1. **Host-neutral (CRITICAL):** every NEW/MODIFIED file passes
   `test_portability.py` (no host path, `python3.N` host pin, literal username,
   or `~/.` absolute).
2. **100% knob coverage:** `docs/configuration.md` covers the entire `KNOBS`
   registry; the extended `test_knob_docs.py` proves it.
3. **No new knob / no new table / no new migration:** 005 adds no config knob,
   no state table, no schema step (data-model.md).
4. **License verifiable:** `pip show` reports MIT; `LICENSE` at root.
5. **Config-in, image-out (Docker):** the bundle is customized by mounting/
   env, not by rebuilding the image (US5-AS2).
6. **Machine-readable tracker:** the issue templates + semver policy are the
   in-repo tracker; a config-breaking change maps to a major bump via the
   `config-breaking-change.yml` → `semver-policy.md` → runbook chain.
7. **Test-first:** the extended guards (C-9) are written red before the docs/
   compose exist; the docs/compose are written to make them pass.
