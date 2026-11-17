# Solution skeleton: 024 — source-grounded retrieval (`sources_only` on `kb_search`)

## `digital_twins/mcp/registry.py`

### `_kb_search_schema`

Gain one optional property in the `properties` dict, alongside the
existing `query` / `limit` / `expand` / `agent_kind` keys:

```python
"sources_only": {
    "type": "boolean",
    "default": False,
    "description": (
        "When true, skip the LLM rerank pass and return the raw "
        "Qdrant hit list as-is (no `digital_twins/llm.py` call). "
        "The `retrieval.llm_rerank` knob is not consulted. "
        "Default false: the LLM rerank runs byte-for-byte as today."
    ),
},
```

`required` stays `["query"]` — the parameter is optional. The tool
description in `build_tool_registry()` gains one sentence:
"When `sources_only: true`, the LLM rerank is skipped and the raw
Qdrant hit list is returned; no LLM call is made."

### Registry test

- `test_mcp_registry.py` — `kb_search` in
  `build_tool_registry()`: `inputSchema.properties` has
  `sources_only` (type `boolean`, default `False`), **not** in
  `required`.
- `test_mcp_registry.py` — tool count unchanged (no new tool added).

## `digital_twins/mcp/dispatch.py`

### `_kb_search_body`

After the `limit` clamp (step 3 in the docstring) and before the
Qdrant `query_points` call (step 7), read and branch:

```python
sources_only = bool(args.get("sources_only", False))
```

When `sources_only` is `True`:

1. The `retrieval.llm_rerank` knob is **not consulted** — the LLM
   rerank pass is skipped entirely; no `digital_twins/llm.py` call
   is made.
2. The raw `query_points` result (the pre-rerank hit list) is
   returned as-is. Each hit row already carries
   `score`, `source_url`, `text`, `full_content`,
   `content_snippet`, `source`, `chunk_index`, `item_id`, `id`
   (the 007 payload mapping, unchanged). The response shape is:

   ```python
   {"ok": True, "hits": [
       {"source": str, "page": int|None, "key": str, "text": str},
       ...
   ]}
   ```

   where `page` is `int` when the 017 page-aware extractor set it,
   `None` otherwise (the documented "no page" sentinel); `key` is
   the `IngestItem.key` (content-addressed dedup id); `text` is
   the extracted chunk text. No match → `{"ok": True, "hits": []}`.

3. NFR-13: only the caller-supplied `source` name string is echoed
   in each hit; no host paths appear in the response.

When `sources_only` is `False` (the default): the body is
byte-for-byte unchanged — the LLM rerank runs, and the response
is the LLM-reranked list under the existing `{"ok": True,
"results": [...], "count": N}` shape.

### Dispatch tests

- `test_mcp_kb_search.py` — `kb_search` with `{"query": "...",
  "sources_only": true}` → mock LLM endpoint: 0 calls recorded;
  response is `{"ok": True, "hits": [...]}` (raw hit list, each
  hit carries `source`, `page`, `key`, `text`).
- `test_mcp_kb_search.py` — `kb_search` with `{"query": "..."}`
  (no `sources_only`) → mock LLM endpoint: 1 call; response is
  the LLM-reranked list (byte-for-byte unchanged from 007).
- `test_mcp_kb_search.py` — `kb_search` with `{"query": "...",
  "sources_only": true}` + mock Qdrant returns no points →
  `{"ok": True, "hits": []}` (no error).
- `test_mcp_kb_search.py` — `kb_search` with `{"query": "...",
  "sources_only": true}` + LLM endpoint down → response is still
  returned (the LLM is never called; the `retrieval.llm_rerank`
  knob is not consulted).

## `GET /api/v1/search` handler (015 HTTP layer)

The 015 HTTP layer routes all MCP tools through
`dispatch(ctx, request.get("tool", ""), request.get("args") or {})`
in `digital_twins/mcp/http.py`. `GET /api/v1/search` is one of
those routes. It already accepts a `limit` query parameter.

Gain: accept a `sources_only` query parameter (boolean, default
`false`) and forward it into the `kb_search` tool args dict
alongside the existing `limit`. No new endpoint; the parameter
is an addition to the same query string.

```
GET /api/v1/search?query=...&limit=5&sources_only=true
```

When `sources_only=true` → the dispatch layer runs `kb_search`
with `sources_only: true` → the raw hit list is returned (no LLM
call). When `sources_only` is omitted or `false` → the LLM-reranked
list is returned (byte-for-byte unchanged from 015).

### HTTP tests

- `test_http_search.py` — `GET /api/v1/search?sources_only=true`
  → mock LLM endpoint: 0 calls; response is the raw hit list
  (same contract as the MCP tool).
- `test_http_search.py` — `GET /api/v1/search` without
  `sources_only` → mock LLM endpoint: 1 call; LLM-reranked list
  (byte-for-byte unchanged from 015).
- `test_http_search.py` — `GET /api/v1/search?sources_only=true&limit=5`
  → `limit` is honoured (the 015 HTTP layer already accepts
  `limit`; `sources_only` is an addition to the same query string).

## Guard tests

- **T006** (`tests/integration/test_portability.py`) — stays green.
  024 introduces no host paths: the response carries only the
  caller-supplied `source` name string (NFR-13). No new file
  paths, usernames, or install locations appear in the three
  source files or the response shape.
- **T027** (`tests/unit/test_knob_docs.py`) — stays green
  unchanged. 024 introduces **no** new config knob: `sources_only`
  is a tool/HTTP parameter, not a knob. `docs/configuration.md`
  needs no change; the knob-docs test does not know about
  `sources_only` and does not need to.
