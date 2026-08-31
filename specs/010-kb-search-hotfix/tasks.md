# Tasks: 010 KB search hotfix (pre-0.9.0-release defect slice)

**Input**: `specs/010-kb-search-hotfix/plan.md`.

**Tests**: included — constitution III (Test-First, NON-NEGOTIABLE):
RED commit failing before the GREEN commit, per task.

## Task 1 — RED: web kb_search embed + QueryResponse shape

- [x] T1 RED: `tests/unit/test_web_app_kb.py` — `FakeQdrantClient`
  records `last_query`; new `_seed_embed_pool` helper (the
  `app._embed_pool` contract, mirroring
  `tests/integration/test_web_app.py::test_e2e_full_journey`);
  `test_kb_search_top_n_owner_scoped` +
  `test_kb_search_qdrant_unavailable` seed the pool (stay green — the
  pre-fix handler ignores the pool); NEW
  `test_kb_search_embeds_query_and_passes_vector` (RED: `last_query`
  is None — the query is never embedded), NEW
  `test_kb_search_query_response_shape` (RED: 500 — the shipped
  handler iterates the `QueryResponse`), NEW
  `test_kb_search_embedding_unavailable_503` (RED: currently 200 — no
  embedding branch exists). Record the RED output in the ledger.

## Task 2 — GREEN: web handler embed + vector + shape

- [x] T2 GREEN: `digital_twins/web/app.py::_handle_kb_search` — resolve
  the qdrant client first (unchanged order for the unavailable
  contract), then embed via `self._pooled_embedder()([query])`
  (batch-with-`.tolist` or plain list; empty → embedding-failure 503),
  pass `query=query_vector` to `query_points`, normalise
  `results.points` (`hasattr` fallback — the CountResult pattern in
  `_count_via_client`), add the `_EMBEDDING_UNAVAILABLE` 503 hint
  (distinct from the qdrant hint). Update the handler docstring.
  Verify: `pytest tests/unit/test_web_app_kb.py
  tests/integration/test_web_app.py -q` green.

## Task 3 — RED + GREEN: MCP kb_search QueryResponse shape

- [x] T3 RED: `tests/unit/test_mcp_kb_search.py` — NEW
  `test_kb_search_query_response_shape` (the fake returns a
  QueryResponse-shaped `.points` object; assert `ok` + the rows).
  Verify RED (direct iteration breaks).
- [x] T3 GREEN: `digital_twins/mcp/dispatch.py::_kb_search_body` —
  `points = results.points if hasattr(results, "points") else results`
  before the rows. Verify: `pytest tests/unit/test_mcp_kb_search.py
  tests/integration/test_mcp_kb_tools.py -q` green.

## Task 4 — contract doc

- [x] T4: `specs/006-web-app/contracts/web-api.md` — `/api/kb/search`:
  add the embedding-failure 503 line + the status-table note (the
  search path embeds the query via the config-pinned model before the
  vector query).

## Verify & close

- [x] V1: standing guards (`tests/integration/test_portability.py`,
  `tests/unit/test_knob_docs.py`) + full suite green (964 + new).
- [x] V2: rebuild sdist + wheel; reinstall into `/tmp/dtkb-pypi`;
  restart the local server; live `POST /api/kb/search` → 200 against
  the live Qdrant (owner with no points → `results: []` still proves
  the full path: embed → vector query → shape).
- [ ] V3: ledger + tasks checkboxes synced; report to owner; re-ask the
  pending `twine upload dist/*` go-ahead (0.9.0 now includes the fix).
