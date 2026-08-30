# Plan: 007 T024 deferred-fix slice (D-007-1..3)

**Input**: `.superpowers/sdd/deferred-minors/007-t024-findings.md`
(the three findings from the T024 whole-branch review, recorded with their
fix notes). This slice fixes them; it is NOT a version bump and adds no
user-visible feature.

**Prerequisites**: main @ `e320eaf` (007 merged, v0.7.0, 831 tests green).
Work happens in worktree `.worktrees/007-t024-followup` (branch
`007-t024-followup`).

**Tests**: included — constitution III (Test-First, NON-NEGOTIABLE):
RED test committed and failing **before** the GREEN fix, per task.

**Success criteria**: all three findings fixed; the previously flaky
ordered run is stably green; the two standing guards
(`tests/integration/test_portability.py`, `tests/unit/test_knob_docs.py`)
stay green; full suite green. Version stays `0.7.0`.

## Global constraints

- No changes to `digital_twins/web/`, `digital_twins/ingest/`,
  `digital_twins/health.py`, `digital_twins/accounts.py`,
  `digital_twins/config/` (same rule as 007).
- No host pins (NFR-13); no new knobs (so `test_knob_docs.py` stays
  vacuous-green).
- No version bump — `digital_twins/__init__.py` stays `0.7.0`.
- Test runner: the repo has no `.venv`; use
  `/home/terry/projects/digital-twins/.venv/bin/python -m pytest` from the
  worktree root (package resolves from the worktree tree).
- Commit after each task (RED commit, then GREEN commit, per constitution
  III); keep this slice's tasks.md checkboxes in sync with commits.

## Task 1 — D-007-1: env-hermetic MCP tests (order-flaky sc005 test)

**Finding**: `tests/integration/test_mcp_integration.py::test_sc005_transport_parity`
sets `os.environ["DT_PERSONAL_TOKEN"]` raw (try/finally restore) and passes
`service_account_email="system"`. Two failure paths:
(a) `DT_SERVICE_TOKEN` leaked into the process env by an earlier test makes
`_resolve_caller` resolve the service account instead of the personal token
→ wrong identity / fail-closed; (b) the sc005 db has no `system` account,
so any resolution to the service email fail-closes with `internal_error`
("stdio transport: no valid credential"). Repro: run
`tests/unit/test_mcp_config_plumbing.py` before
`tests/integration/test_mcp_integration.py` in one pytest process → sc005
FAILS. 100% reproducible when the planner ran it.

**RED (commit 1)**:
- In `tests/unit/test_mcp_config_plumbing.py`: convert
  `_service_token_for(db)` to `_service_token_for(db, monkeypatch)` and
  replace `os.environ["DT_SERVICE_TOKEN"] = token` with
  `monkeypatch.setenv("DT_SERVICE_TOKEN", token)`; update all call sites in
  that file.
- In `tests/integration/test_mcp_integration.py::test_sc005_transport_parity`:
  add a `monkeypatch` parameter; replace the raw
  `os.environ["DT_PERSONAL_TOKEN"] = bob_token` + try/finally restore with
  `monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)` and
  `monkeypatch.setenv("DT_PERSONAL_TOKEN", bob_token)`.
- **Do NOT** change any other test's token handling in these two files;
  other `os.environ` writes in the suite are out of scope for this slice.
- Red check: `pytest tests/unit/test_mcp_config_plumbing.py
  tests/integration/test_mcp_integration.py -q` in one process must now
  pass (the monkeypatch conversions ARE the fix; the "red" evidence is the
  pre-fix ordered run failing — record that in the commit message). If the
  conversion is already correct at commit time, capture the pre-fix failure
  output first (`git stash` the two edits, run, record, unstash) so the
  ledger has a genuine RED line.

**GREEN (commit 2)**: nothing beyond the test edits — this finding is
test-only. Verify:
- Ordered run TWICE: `pytest tests/unit/test_mcp_config_plumbing.py
  tests/integration/test_mcp_integration.py -q` (both invocations green).
- Full suite green.

## Task 2 — D-007-2: keyed + locked `_qdrant_client_cache`

**Finding**: `digital_twins/mcp/dispatch.py` lines ~623–632 hold a
`_qdrant_client_cache` keyed only on URL; two callers with the same URL but
different api-key / timeout / https knobs share one client (silent
misconfiguration); the check-then-set is racy under concurrent first calls.

**RED (commit 3)**: new `tests/unit/test_mcp_qdrant_cache.py`:
- A client is built **once** per distinct key and reused on the second
  call for the same key (spy on the qdrant client constructor at the
  module seam; call twice, assert one construction).
- Two configs that differ ONLY in `qdrant.api_key` (or https / timeout —
  pick whichever knobs the resolver actually reads from config; verify at
  the seam) produce **two** distinct clients.
- Same config → same object (cache identity), no second construction.

**GREEN (commit 4)**: `digital_twins/mcp/dispatch.py` — replace the
flat cache with a dict keyed on a hashable tuple of the config-derived
connection params (url + every other param the client constructor takes
from config: api_key, https, timeout, ...) guarded by a `threading.Lock`
(atomic check-then-set). Remove or re-key any stale entries; no changes to
the client's behavior, only the cache key + lock. Verify:
`pytest tests/unit/test_mcp_qdrant_cache.py
tests/unit/test_mcp_kb_search.py tests/unit/test_mcp_kb_ingest.py
tests/integration/test_mcp_kb_tools.py -q` green (the seam tests
must still pass — the fake-client seed path is unaffected).

## Task 3 — D-007-3: nested default merge for `kb_ingest`

**Finding**: `_kb_ingest_body` (dispatch.py lines ~984–987) overlays
`ctx.config` onto schema defaults with a FLAT dotted-key merge, while 006
`web/app.py::_merge_defaults` is a nested dict merge. A user config that
sets only `chunking.max_chars` (nested under `chunking`) loses the
`chunking.*` defaults that the sibling keys would have provided — e.g.
`chunking.overlap`/`chunking.min_chars` fall out of the merged config,
silently changing pipeline behavior.

**RED (commit 5)**: extend `tests/unit/test_mcp_kb_ingest.py` (or add a
focused test module if cleaner): a `ctx.config` that sets ONE nested value
under an existing default subtree (e.g. `{"chunking": {"max_chars": 999}}`)
→ the merged config passed to `run_pipeline` (captured by the existing
fake `run_pipeline` seam) contains BOTH the override AND the sibling
defaults from the schema (e.g. `chunking.overlap` / `chunking.min_chars`
at their schema values). RED against the current flat merge.

**GREEN (commit 6)**: `digital_twins/mcp/dispatch.py` — add a
`_deep_merge(base, override)` helper (recursive dict merge, override wins
per key, non-dict leaves replace wholesale — mirroring 006's
`_merge_defaults` semantics; do NOT import from `web/` — that package is
off-limits and the web app is a separate distribution) and use it for
`merged_cfg` in `_kb_ingest_body`. All other body behavior unchanged.
Verify: `pytest tests/unit/test_mcp_kb_ingest.py
tests/integration/test_mcp_kb_tools.py -q` green.

## Verification (after all tasks)

- Full suite: `/home/terry/projects/digital-twins/.venv/bin/python -m
  pytest -q` → all green (expect 831 + new tests).
- Standing guards: `pytest tests/integration/test_portability.py
  tests/unit/test_knob_docs.py -q` green.
- Ordered flake re-run: `pytest tests/unit/test_mcp_config_plumbing.py
  tests/integration/test_mcp_integration.py -q` green ×2.
- `git diff e320eaf..HEAD --stat` touches only: `digital_twins/mcp/dispatch.py`,
  the three test files named above, this plan's `tasks.md`, and CHANGELOG
  is NOT touched (no user-visible change; skip CHANGELOG for a defect
  slice — record that ruling in the ledger).

## Merge gate

STOP after the whole-branch review: the branch must NOT be merged to main
without the owner's explicit go-ahead (project convention + SDD stop
condition).
