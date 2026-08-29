# Quickstart / Validation Guide: Portable Package Foundation

Runnable validation scenarios proving the slice works end-to-end. Assumes the feature is implemented per `plan.md`.

## Prerequisites

- Python ≥ 3.11, `pip`, network access
- A reachable Qdrant + Neo4j + OpenAI-compatible LLM endpoint (local or self-hosted). The default test suite needs none: Qdrant runs in-memory and Neo4j/LLM are stubbed.

## Scenario 1 — Clean-host init (US1, NFR-12)

```bash
time (pip install -e . && digital-twins init && digital-twins validate)
```

**Expected**: config dir created with `kb.local.yml` (every source `enabled: false`); state dir + `state.db` created; health table printed; exit 0. Re-running `init` keeps existing values, prompts for nothing new, exits 0 (idempotent). Total wall time should be under 30 minutes on a clean host with network access (SC-001 / NFR-12).

## Scenario 2 — Validate + dimension guard (US4, NFR-2)

```bash
digital-twins validate
```

**Expected**: exit 0, all endpoints ok. Then point `qdrant.url` at a collection with a different vector size → **hard error** naming the mismatch plus the remediation ("re-embed, or point at a new collection"), exit 1.

## Scenario 3 — Ingest + idempotent re-run (US2, Constitution II, NFR-1/14)

```bash
# enable the fs source on a directory of two .md files in kb.local.yml:
#   sources.fs: { enabled: true, extra: { dir: /tmp/kb-demo } }
digital-twins run --source fs
digital-twins run --source fs      # second run
```

**Expected**: run 1 → `fs: new=2, skipped=0`, audit row with `run_id` written; run 2 → `new=0, skipped=2`; the collection still holds exactly 2 points. (Test: `tests/integration/test_idempotency.py`.)

## Scenario 4 — Fail-fast prerequisite (US2, BR-11.2.2)

```bash
# kb.local.yml: sources.hermes.enabled: true (no session store present on this host)
digital-twins run --source hermes
```

**Expected**: exit **2**, message names the source + missing prerequisite + where to set it. Nothing ingested; the failed run is still audited.

## Scenario 5 — Custom source without a package update (US5, FR-011)

```yaml
# kb.local.yml
sources.mytool:
  enabled: true
  entrypoint: mytool_kb:make_source     # user module on PYTHONPATH
  credential: MYTOOL_TOKEN
  prefix: "mytool:"
```

**Expected**: with `MYTOOL_TOKEN` set, `digital-twins run --source mytool` ingests; without it, fail-fast exit 2 naming the credential. No package reinstall involved.

## Scenario 6 — Portability + knob-doc audit (SC-002, SC-003, NFR-13)

```bash
pytest tests/integration/test_portability.py tests/unit/test_knob_docs.py
```

**Expected**: both pass — zero host-specific paths/usernames in shipped code, config, docs; zero undocumented knobs.

## Scenario 7 — Upgrade preservation (NFR-15)

Covered by `tests/unit/test_upgrade.py`: seed state (high-water + audit + accounts), run a version-bumped migration, assert every row and the config survive.

## Test commands

```bash
pytest tests/unit -q                       # hermetic default suite
pytest tests/integration -q                # in-memory Qdrant + stubs
KB_LIVE_QDRANT=... KB_LIVE_NEO4J=... pytest -m live   # opt-in real services
```
