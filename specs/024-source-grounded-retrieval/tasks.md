# Tasks: 024 — source-grounded retrieval (`sources_only` on `kb_search`)

## 1. Unit tests (RED-first, constitution Principle III)

- [ ] 1.1 Write unit tests in `tests/unit/test_mcp_kb_search.py`
      (extend the existing `kb_search` fixture) covering:
      - `kb_search` with `{"query": "...", "sources_only": true}` →
        the mock LLM endpoint is **not** called (0 calls recorded);
        the response is the raw Qdrant hit list (each hit carries
        `source`, `page`, `key`, `text`).
      - `kb_search` with `{"query": "..."}` (no `sources_only`, or
        `sources_only: false`) → the mock LLM endpoint **is** called
        (1 call); the response is the LLM-reranked list
        (byte-for-byte unchanged from 007).
      - `kb_search` with `{"query": "...", "sources_only": true}` +
        no matching chunks in the mock Qdrant → `{"ok": true, "hits":
        []}` (no error).
      **Verification:** tests fail on current code (the
      `sources_only` parameter does not exist yet). No host paths
      (NFR-13) — use `tmp_path` fixtures.

- [ ] 1.2 Write a unit test in `tests/unit/test_http_search.py`
      (new file) for `GET /api/v1/search?sources_only=true`:
      - the same contract as the MCP tool (raw hit list, 0 LLM
        calls).
      - `GET /api/v1/search` without `sources_only` → LLM-reranked
        list (byte-for-byte unchanged from 015).
      **Verification:** test fails on current code (the parameter
      is not yet accepted by the HTTP layer).

## 2. Schema (MCP tool)

- [ ] 2.1 In `digital_twins/mcp/registry.py::_kb_search_schema()`:
      add an optional boolean property `sources_only` (default
      `false`) alongside the existing `query` / `limit` / `expand` /
      `agent_kind` properties. `required` stays `["query"]`. Update
      the tool description to document the skip-LLM behaviour.
      **Verification:** a registry test asserts `sources_only`
      appears in `build_tool_registry()` with type `boolean`,
      default `false`, and is **not** in `required`.

## 3. Tool body (MCP dispatch)

- [ ] 3.1 In `digital_twins/mcp/dispatch.py::_kb_search_body`:
      read `args.get("sources_only")` and coerce to `bool` (default
      `False`). When `True`: skip the LLM rerank pass entirely
      (the `retrieval.llm_rerank` knob is **not** consulted; no
      `digital_twins/llm.py` call) and return the pre-rerank Qdrant
      hit list as-is. When `False`: the body is byte-for-byte
      unchanged (the LLM rerank runs as today). **Verification:**
      §1.1 tests green.

- [ ] 3.2 Response shape when `sources_only: true`: each hit in the
      `hits` array carries `{"source": <string>, "page": int|null,
      "key": <string>, "text": <string>}`. No match → `{"ok": true,
      "hits": []}`. NFR-13: only the caller-supplied `source` name
      is echoed; no host paths. **Verification:** §1.1 assertion
      on hit shape passes.

## 4. HTTP layer (015 transport)

- [ ] 4.1 In the 015 HTTP layer that serves `GET /api/v1/search`:
      accept a `sources_only` query parameter (boolean, default
      `false`) and forward it into the `kb_search` tool args. The
      `limit` query parameter is already accepted; `sources_only`
      is an addition to the same query string. **Verification:**
      §1.2 tests green.

## 5. Guards + full suite

- [ ] 5.1 Guards: `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → both green. T006 (no
      host paths introduced) and T027 (no new config knob, so
      `docs/configuration.md` needs no change) stay green.
      **Verification:** both guard files pass unchanged.

- [ ] 5.2 Full suite: `python3 -m pytest tests/ -q` → all green.

## 6. Semver + release note

- [ ] 6.1 Bump `__version__` (MINOR) in `digital_twins/__init__.py`
      + `CHANGELOG.md` entry: "Added: `sources_only` parameter on
      `kb_search` and `GET /api/v1/search` — when `true`, the LLM
      rerank is skipped and the raw Qdrant hit list is returned as-
      is (feature 024; default-off)."

## 7. Bookkeeping

- [ ] 7.1 `specs/024-source-grounded-retrieval/` — spec / plan /
      tasks / solution artifacts created and in sync with this
      task list.

- [ ] 7.2 `.superpowers/sdd/024-source-grounded-retrieval/progress.md`
      ledger created and kept in sync with commits.

## Verification summary

- `pytest tests/unit/test_mcp_kb_search.py tests/unit/test_http_search.py
  tests/unit/test_mcp_registry.py -q` → all passed (parameter
  present in schema, body correct, HTTP forwarding correct).
- `pytest tests/ -q` → all green.
- `pytest tests/integration/test_portability.py tests/unit/test_knob_docs.py
  -q` → both guards green (T006 + T027).
