# Quickstart / Validation Guide: Community Packaging & Documentation

The 5-minute install → init → first-run path exactly as it will appear in
`README.md` (US1 / SC-001), plus the packaging validation scenarios that prove
each 005 deliverable. Host-neutral throughout (constitution I, NFR-13):
placeholders only — `<STATE_DIR>`, `<A DIR OF .md FILES>`, no literal host path,
username, or interpreter pin.

> **Command-name note (research §b1):** the spec says "health passes from
> inside." 001–004 ship **no standalone `health` command** — health checks run
> inside `init` and `validate`. This quickstart uses **`digital-twins validate`**
> (the health-report command) and states the equivalence. If a literal `health`
> subcommand is desired, that is a follow-up CLI addition, not a 005 packaging
> change.

---

## 0. Conventions (host-neutrality, NFR-13 / SC-006)

- No host paths, usernames, or interpreter pins. Placeholders only:
  `<STATE_DIR>`, `<A DIR OF .md FILES>`, `<EMAIL>`, `<PASSWORD>`.
- **State directory:** the `KB_STATE_DIR` env var (the `state_dir` knob). All
  checks use `$KB_STATE_DIR` — never a literal path.
- **CLI:** `digital-twins` (the console script installed by `pip install .`).
- **Credentials are env vars, never argv:** `KB_*` knobs, `INIT_ADMIN_*`
  (first-admin), `DT_USER_PASSWORD` / `DT_PERSONAL_TOKEN` (002/003 auth).

---

## The 5-minute path (US1 / SC-001, the README quick start)

**Given:** a clean machine (a test harness or a fresh VM) with Python ≥ 3.11,
no existing KB state, no host context.

```bash
# 0) a fresh state dir (the state_dir knob; never a hardcoded host path)
export KB_STATE_DIR="<STATE_DIR>"

# 1) install — from PyPI once published; from the source tree until then.
#    (hatchling build; the console script `digital-twins` is installed.)
pip install digital-twins        # published
# — or, from a clone of the repo:
pip install .                    # source tree (hatchling)

# 2) init: prompts for qdrant.url, neo4j.url/user/password, llm.endpoint;
#    creates the state dir + state.db; writes a starter kb.local.yml with
#    every source disabled (BR-11.2.7); runs the health report.
#    Non-interactive (CI / host-cron): set the endpoint + INIT_ADMIN_* env vars.
digital-twins init
# expect: endpoints reported; "created first admin account <EMAIL>";
#         a health table; exit 0. Re-running init is idempotent (exit 0, no prompt).

# 3) validate: exits 0 only when Qdrant + Neo4j + LLM are all reachable.
digital-twins validate
# expect: exit 0 (all endpoints ok).
#   point qdrant.url at a collection with a different vector size -> HARD ERROR
#   naming the mismatch + remediation ("re-embed, or point at a new collection"),
#   exit 1.

# 4) first run: enable the fs demo source (a directory of .md files; no
#    credential needed) in kb.local.yml, then ingest once.
#   kb.local.yml:
#     sources:
#       fs:
#         enabled: true
#         extra: { dir: "<A DIR OF .md FILES>" }
digital-twins run --source fs
# expect: "fs: N item(s)"; an audit row with a run_id.
digital-twins run --source fs
# expect: "fs: 0 item(s)" — the second run dedups to zero (NFR-1 one-record).
```

**Success criteria (SC-001):** on a clean machine, following the README
quickstart verbatim reaches a passing first run in < 5 minutes (a first `init`
+ `validate` + a `run --source fs` that ingests and dedups). This is the
community-promise test: a fresh human, no host context, succeeds without reading
source.

**Automated form:** the 5-minute path itself is a *manual* acceptance scenario
(SC-001 / US1 — a clean-machine human follows the README). The *mechanics* it
depends on are each automated: install/`init` (001 `tests/`), `validate`
health checks (001 `health.py` tests), `fs` source dedup (001 one-record
guard). 005 adds no new automated test for the path — it documents it and keeps
the existing guards green.

---

## Scenario A — Configuration reference: look up any shipped knob (US1-AS2, SC-003)

**Given:** `docs/configuration.md` (the 100% knob reference).
**When:** a user looks up any shipped knob — including 002's
`scheduler.status_port` and 001's full surface —
**Then:** it is documented with default, type, and env-var mapping.

```bash
# the automated proof (the extended knob-doc guard):
pytest tests/unit/test_knob_docs.py
# expect: green — every KNOBS registry knob appears in docs/configuration.md
#         with a matching env var + default; no phantom knob.
```

**Manual spot-check:** open `docs/configuration.md`, find `scheduler.status_port`
(→ default `8765`, env `KB_SCHEDULER__STATUS_PORT`), find
`embedding.model` (→ default `BAAI/bge-small-en-v1.5`, 384-dim), find
`sources.fs.enabled` (→ default `false`, env `KB_SOURCES__FS__ENABLED`). Each is
present with its type, default, and env mapping.

---

## Scenario B — Add a new source without a package update (US1-AS3, BR-11.6.1)

**Given:** the "add a new source" how-to in the README.
**When:** a user registers a custom entry-point source,
**Then:** they can run it and see it in `validate`/`run` (the 001 US5 path,
documented end-to-end).

```bash
# kb.local.yml: register a custom source "mytool"
#   sources:
#     mytool:
#       enabled: true
#       entrypoint: mytool_kb:make_source   # importable module:factory
#       credential: MYTOOL_TOKEN
#       prefix: "mytool:"
export MYTOOL_TOKEN="<TOKEN>"
digital-twins run --source mytool
# expect: ingests via the user's module (no package reinstall).
unset MYTOOL_TOKEN
digital-twins run --source mytool
# expect: fail-fast exit 2, message names the source + the missing
#         credential env var (BR-11.2.4/2.7 — never silently ingest zero).
```

---

## Scenario C — MIT license, machine-verifiable (US2, SC-002)

**Given:** the built wheel.
**When:** it is installed,
**Then:** `pip show` reports `License: MIT`.

```bash
# in a clean venv (the automated proof):
python -m build --wheel                       # -> dist/digital_twins-<v>-py3-none-any.whl
pip install dist/digital_twins-<v>-py3-none-any.whl
pip show digital-twins | grep -i license       # -> License: MIT
```

**Also:** a license detector (e.g. `licensecheck` / GitHub license API) identifies
MIT at the root (`LICENSE` present). **Manual:** the TestPyPI/PyPI round-trip
(SC-006 manual) — see `docs/release-runbook.md`.

---

## Scenario D — Versioning policy + tracker surface (US3, SC-004)

**Given:** the repo.
**When:** a new issue/feature is filed,
**Then:** it uses a documented in-repo template with labeling/conventions (no
live GitHub dependency required).

```bash
# the in-repo tracker (offline, no GitHub remote):
ls .github/ISSUE_TEMPLATE/
#   config.yml  bug_report.yml  feature_request.yml  config-breaking-change.yml
cat docs/semver-policy.md   # names the rules: config breaking = major + migration note
```

**Config-breaking change → major bump (US3-AS2, SC-004):** a change proposed via
`config-breaking-change.yml` (required `version_impact: major` + required
`migration_note`) forces a major bump; at release, the runbook cross-checks that
the changelog's breaking entry (SC-004: every release since 0.1.0 has a
`CHANGELOG.md` entry) carries the migration note. The *005 slice itself* is a
`minor` bump (`0.4.0`) — it adds docs/packaging, no config-breaking change.

---

## Scenario E — Docker bundle: in-container health passes (US5, SC-005)

**Given:** a Docker-capable host, the shipped `docker-compose.yml` + `Dockerfile`.
**When:** the bundle is brought up,
**Then:** all services report healthy and `digital-twins validate` (in-container)
exits 0; customization is by config-in, not image rebuild.

```bash
# build the digital-twins image from the repo (manual: needs a Docker host)
docker build -t digital-twins:0.3.0 .

# bring up the five-service bundle (qdrant, neo4j, llm, embedding-model, digital-twins)
docker compose up -d
docker compose ps        # expect: all 5 services "healthy"

# in-container health proof (the SC-005 manual check):
docker compose exec digital-twins digital-twins validate
# expect: exit 0 (Qdrant + Neo4j + LLM all reachable from inside the network)

# config-in, not image-out (US5-AS2): change the bundle by mounting kb.yml /
# setting env, never by rebuilding the image. E.g. point at an external LLM:
#   docker compose up -d with LLM_ENDPOINT=<EXTERNAL OPENAI-COMPATIBLE URL>
```

**Automated form (SC-005 automated):** `docker-compose.yml` is well-formed YAML
with the five required services, all pinned (no `:latest`), config-in mounts,
and zero host-path / host-pin leakage (portability guard extended to the
compose file). The live `docker compose up` is the **manual** proof (needs a
Docker host).

**D-1 / BR-11.1.6 (the LLM + embedding are bundled):** the `llm` service
(SGLang or equivalent) and the `embedding-model` service (pinned
`BAAI/bge-small-en-v1.5`) run **in the bundle by default** — a user with no
external LLM/embedding service runs end-to-end from the container. External
endpoints are an **opt-in** env override (`LLM_ENDPOINT`, `EMBEDDING_ENDPOINT`);
the in-network defaults are the bundled services.

---

## Scenario F — PyPI publication mechanics (US6, SC-006)

**Given:** `docs/release-runbook.md`.
**When:** a maintainer follows it,
**Then:** the package is testable on TestPyPI and publishable to PyPI.

```bash
# the automated proof (in-suite):
python -m build          # wheel builds; pip show reports MIT; runbook exists
# the manual round-trip (needs network + credentials — docs/release-runbook.md):
twine upload --repository-url https://test.pypi.org/legacy/ dist/*
pip install --index-url https://test.pypi.org/simple/ digital-twins
# -> works; then the real PyPI publish (same command, no --repository-url).
```

---

## Exit criteria

All scenarios pass; `pytest` green; `tests/integration/test_portability.py`
green (no host paths / interpreter pins / literal usernames — the new 005
artifacts: `docs/configuration.md`, `docs/semver-policy.md`,
`docs/release-runbook.md`, `docs/references/agent-guides.md`,
`docker-compose.yml`, `Dockerfile`, `.github/ISSUE_TEMPLATE/` are all checked);
`tests/unit/test_knob_docs.py` green (100% knob coverage of
`docs/configuration.md`); `pip show` reports MIT; `docker-compose.yml` is
well-formed, five services, pinned, host-neutral; `docs/release-runbook.md`
exists. The 5-minute path (Scenario 0) is the manual acceptance; the rest are
the automated proofs. All host-neutral (SC-006).
