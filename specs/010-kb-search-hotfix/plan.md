# Plan: 010 KB search hotfix (pre-0.9.0-release defect slice)

**Input**: Live local-deploy repro (2026-08-31, `/tmp/dtkb-deploy`, 0.9.0
wheel): `POST /api/kb/search` → **500** `{"error":"internal_error"}`
against a **live** Qdrant (`http://pop-os:6333`, v1.18.0; collection
`personal_kb` = 656,953 points, 384-dim Cosine) with `qdrant.url`
correctly present in the server's startup config snapshot.

The 503 `"qdrant unavailable: check qdrant.url (env: KB_QDRANT__URL)..."`
the owner saw earlier is the **documented** startup-snapshot case (009
spec, the two-config-semantics rule: probe routes re-read config per
request, the KB data path uses the startup snapshot + cached client):
the server instance had started before the URL was saved. That is
config/restart behaviour, not a defect — and it no longer reproduces on
the restarted server. What the restart exposed is the defect below.

## Root cause (confirmed against the installed wheel + live Qdrant)

1. **`web/app.py::_handle_kb_search` never embeds the query** (006
   T008/T010 surface). The docstring claims "the query is embedded via
   the config-pinned embedding model"; the code calls
   `client.query_points(QDRANT_COLLECTION, query_filter=..., limit=...,
   with_payload=True)` with **no `query=<vector>`** and **no embedder
   call**. The MCP counterpart (`mcp/dispatch.py::_kb_search_body`, 007)
   does it right: pooled `_embed_query` + `query=query_vector`.
2. **Result-shape mismatch (web + MCP)**: real qdrant-client 1.18
   `query_points` returns a `QueryResponse` (points under `.points`);
   iterating it yields pydantic `(field, value)` tuples, so `r.score`
   → AttributeError. Web: the rows are built **outside** the
   try/except → unhandled → 500 `internal_error` (reproduced live in
   ~0.8 s). MCP: same direct iteration → error result. All 964 tests
   stayed green because every test fake returns a bare list and
   accepts `query=None`.
3. (consequence) The web surface has no embedding-failure branch, so the
   fail-closed 503-hint contract has no embedding counterpart (007 has
   `embedding_unavailable`).

## Success criteria

- `POST /api/kb/search` against a live Qdrant returns 200 with the
  owner-scoped top-N (an owner with no points → 200 with `results: []`),
  the query embedded via the config-pinned model, the vector passed as
  `query=`.
- QueryResponse-shaped **and** bare-list client returns both handled
  (web + MCP).
- Embedding load/encode failure → clean 503 naming
  `embedding.model` / `embedding.device`, distinct from the qdrant
  hint (web).
- Standing guards + full suite green. No new knobs. No version bump —
  stays `0.9.0` (unreleased; ships in the 0.9.0 upload).
- Live local redeploy passes a real search end-to-end.

## Global constraints

- No new config knobs (`test_knob_docs.py` stays vacuous-green).
- No host pins (NFR-13; `test_portability.py` stays green).
- No version bump, no CHANGELOG entry for a defect slice (ruling, per
  the 007-t024-followup precedent) — recorded in the ledger.
- Work directly on `main` (hotfix for the unreleased 0.9.0; matches the
  009-closeout commit precedent; the owner's live re-test is the stop
  gate before any PyPI upload).
- Test runner: `.venv/bin/python -m pytest` from the repo root (package
  resolves from the tree; system pip is PEP 668-locked).
- Commit after each task (RED commit, then GREEN commit, per
  constitution III); keep this slice's tasks.md checkboxes in sync with
  commits.
- Contract doc update: `specs/006-web-app/contracts/web-api.md`
  `/api/kb/search` gains the embedding-failure 503 line (additive — the
  502/503 class is already reserved for the search path).
