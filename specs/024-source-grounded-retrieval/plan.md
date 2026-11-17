# Plan: 024 — source-grounded retrieval (`sources_only` on `kb_search`)

## Summary

Adds an opt-in `sources_only: bool = false` parameter to the existing
`kb_search` MCP tool and the existing `GET /api/v1/search` endpoint
(015's HTTP layer). When `true`, the 007 LLM rerank pass is skipped
entirely — the `retrieval.llm_rerank` knob is not consulted and no LLM
call is made — and the raw Qdrant hit list is returned as-is, each hit
carrying `source`, `page`, `key`, and the chunk text. The feature
ships **default-off**: when the parameter is omitted or `false`, the
007 tool body is byte-for-byte unchanged (the LLM rerank runs, the
response is the LLM-reranked list). NFR-1 (dedup) holds: the Qdrant
query is unchanged — same index, same `limit`, same content-addressed
point ids; only the response shape changes. No new config knob, no new
LLM prompt, no new Qdrant index.

## Approach

1. **Schema** (`digital_twins/mcp/registry.py`): `_kb_search_schema()`
   gains one optional boolean property `sources_only` (default
   `false`) alongside the existing `query` / `limit` / `expand` /
   `agent_kind` properties. `required` stays `["query"]`. The tool
   description documents the skip-LLM behaviour.
2. **Tool body** (`digital_twins/mcp/dispatch.py`):
   `_kb_search_body` reads `args.get("sources_only")` (coerced to bool,
   default `false`). When `True`: the LLM rerank pass is skipped
   (the `retrieval.llm_rerank` knob is **not** consulted; no
   `digital_twins/llm.py` call), and the pre-rerank Qdrant hit list
   is returned as-is. When `False` (the default): the body is
   byte-for-byte unchanged.
3. **HTTP layer** (`GET /api/v1/search`, 015's HTTP layer): accepts a
   `sources_only` query parameter that is forwarded into the `kb_search`
   tool args (the 015 layer already accepts `limit`; `sources_only`
   is an addition to the same query string). No new endpoint.
4. **Response shape**: when `sources_only: true`, the response is
   `{"ok": true, "hits": [{"source": ..., "page": int|null, "key":
   ..., "text": ...}]}` (no match → `{"ok": true, "hits": []}`).
   NFR-13: no host paths in the response — only the caller-supplied
   `source` name is echoed.
5. **Tests (Test-First, constitution Principle III)**: unit tests with
   a mock Qdrant fixture assert (a) `sources_only: true` → 0 LLM
   calls, raw hit list returned; (b) `sources_only: false` (default) →
   1 LLM call, LLM-reranked list byte-for-byte unchanged; (c)
   `sources_only: true` + no matching chunks → `{"ok": true, "hits":
   []}`; (d) `GET /api/v1/search?sources_only=true` → same contract as
   the MCP tool. Guards: `test_portability.py` (T006) and
   `test_knob_docs.py` (T027) stay green — no new knob, so
   `docs/configuration.md` is unchanged.

## Files touched

- `digital_twins/mcp/registry.py` (schema: add `sources_only` property)
- `digital_twins/mcp/dispatch.py` (body: read + branch on the parameter)
- `GET /api/v1/search` handler (015's HTTP layer — forward the query
  parameter into tool args)
- `tests/unit/` (kb_search + kb_search_sources_only test outlines)
- `tests/integration/test_portability.py` (T006 guard — no change,
  stay green)
- `tests/unit/test_knob_docs.py` (T027 guard — no change, stay green)
- `CHANGELOG.md` + `digital_twins/__init__.py` (MINOR version bump)

## Open questions

- Should `sources_only: true` also disable the 007 LLM *answer* pass
  (`kb_chat`)? — Default: no; the `kb_search` hit-list skip is
  sufficient for v1 (a `kb_chat` source-grounded answer is a separate,
  larger feature — v1.1).
- Should `GET /api/v1/search` with `?sources_only=true` also accept a
  `limit` query parameter? — Default: yes (the 015 HTTP layer already
  accepts `limit`; `sources_only` is an addition to the same query
  string).

## Semver

MINOR. New optional query parameter on an existing tool and an
existing endpoint; when `sources_only` is omitted or `false`, the
response is byte-for-byte unchanged from 007 / 015. No new config
knob, no new LLM call, no new Qdrant index — NFR-1 dedup holds.
