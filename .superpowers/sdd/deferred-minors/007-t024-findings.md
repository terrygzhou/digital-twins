# Deferred — 007 T024 review findings (post-merge follow-up slice)

Source: T024 whole-branch review of `007-kb-search-chat`
(`7fd350b..26b3f25`), recorded in
`.superpowers/sdd/007-kb-search-chat/progress.md` Phase 5. Branch merged;
all findings non-blocking. One follow-up slice should close all three code
items below (they are small and share the same review context).

## D-007-1 (Important) — order-flaky `test_sc005_transport_parity`

- Where: `tests/integration/test_mcp_integration.py:352` (004 test,
  unmodified by 007).
- Symptom: 101/102 in the 007-mandated 10-file subset run; the stdio leg
  fails with `internal_error` "stdio transport: no valid credential
  (account 'system' does not exist)" even though the test's own DB has a
  `system` account. 100% reproducible in that subset order; 100% green in
  isolation (6/6) and in the full suite (831/831).
- Root cause: `mcp_stdio.serve` credential resolution reads process-global
  env/credential state; an earlier test in the subset (e.g.
  `test_mcp_config_plumbing.py`, which monkeypatches the config loader and
  exercises `stdio.main`) leaves that state dirty.
- Fix (either): (a) autouse fixture in `tests/integration/conftest.py`
  that snapshots + restores credential-related process globals around every
  integration test, or (b) inject an explicit credential seam in the 004
  test so the stdio leg never touches process env.
- Verify: the 10-file subset green twice in a row (order-sensitive, so
  run it as the regression gate), plus full suite.

## D-007-2 (Minor) — unkeyed `_qdrant_client_cache`

- Where: `digital_twins/mcp/dispatch.py:623–632`.
- Symptom: module-global cache has no key; a second `MCPContext` with a
  different `qdrant.url`/`api_key` reuses the first client. Check-then-set
  is also unguarded (benign race under threaded HTTP).
- Fix: key the cache on `(url, api_key)` (dict of clients) + a lock around
  construction; add one test asserting a config change yields a fresh
  client.

## D-007-3 (Minor) — `_kb_ingest_body` flat-overlay merge shape

- Where: `digital_twins/mcp/dispatch.py:984–987`.
- Symptom: overlays the flat dotted `config.schema.DEFAULTS` directly onto
  the nested `ctx.config` → hybrid nested+flat dict. Diverges from 006's
  `web/app.py::_merge_defaults` (expands DEFAULTS to nested first, then
  deep-merges caller-wins). Functionally equivalent today (all pipeline
  reads go through `config.schema.get`); latent for any nested-section
  consumer.
- Fix: duplicate the ~10-line nested expansion from `web/app.py` into
  `dispatch.py` before the overlay (factoring into a shared helper would
  touch `web/`, which 007's scope forbade — acceptable for the follow-up
  slice; the shared-helper refactor is open if preferred).

## Bookkeeping (closed, no follow-up needed)

- T024 finding I4 — `specs/007-kb-search-chat/tasks.md` checkbox drift
  (T001–T004, T010–T017, T023, T024): fixed in the same commit that
  recorded the T024 review in the SDD ledger.

## Notes

- `kb_chat` generation follow-up (501 surface → real LLM call) is a
  separate planned slice, not a review finding; its two-branch read is
  already decision-ready as designed (branch on `llm.endpoint`/`llm.model`).
