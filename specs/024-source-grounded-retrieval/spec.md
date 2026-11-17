# Feature 024: Source-grounded retrieval (a `sources_only` flag on `kb_search`)

> **License note** (NFR-13 / constitution I): this spec is an
> inspiration-level design reference only. No code, prompts, or test
> fixtures are ported from `nashsu/llm_wiki` (GPL-3.0) into this MIT
> package.

## Background

007's `kb_search` returns raw Qdrant chunk hits (each tagged
with `source`, `page`, `key`) plus, when the optional LLM
rerank is enabled (`retrieval.llm_rerank: true`, the default),
a *reranked* order. The rerank is model-driven and can
reorder hits in a way that is hard to explain (the LLM
re-orders by "relevance" as it sees it). The gap doc's
`nashsu/llm_wiki` reference keeps a *strictly source-grounded*
retrieval path — the operator wants the raw Qdrant hits
returned as-is, with no LLM involvement, for audit and for
debugging the index. This feature adds a single, opt-out
`sources_only` flag to `kb_search` (and `GET /api/v1/search`):
when true, the LLM rerank is skipped and the raw hits are
returned.

## User Scenarios & Testing

An operator ingests 500 documents and wants to *verify* that
the Qdrant index is correct — no LLM re-ordering, no surprise
ranking. They call `kb_search` with `sources_only: true`
(today `kb_search` has no such parameter; this feature adds
it). The response is the raw Qdrant hit list, each entry
carrying `source`, `page`, `key`, and the extracted chunk
text (the 007 hit contract, just without the LLM re-ordering
pass).

**Primary scenario**

1. `retrieval.llm_rerank: true` (default) — the LLM rerank is
   *available* but *skipped* when `sources_only: true`.
2. `kb_search` with `{"query": "...", "sources_only": true}`:
   - the Qdrant query runs exactly as today (same index,
     same `limit`).
   - the LLM rerank pass is *skipped* (no `digital_twins/
     llm.py` call).
   - each hit in the response carries the *raw* Qdrant
     fields: `source` (the 007 source-name string, e.g.
     `"fs"` / `"hermes"` / `"paperclip"` / `"pi"` /
     `"dsh"` / `"imap_mail"` / `"custom"`), `page` (a 1-based
     page number when the extractor is 017's page-aware;
     `null` for non-page-aware sources), `key` (the
     `IngestItem.key` — the content-addressed dedup id), and
     the extracted chunk text.
   - no new LLM call (the `retrieval.llm_rerank` knob is
     *not consulted* when `sources_only: true`).
3. `kb_search` with `{"query": "..."}` (no `sources_only`,
   or `sources_only: false`) — the LLM rerank runs exactly
   as today (byte-for-byte unchanged).

**Edge cases**

- `sources_only: true` with no matching chunks in Qdrant →
  an empty hits array (the existing `{"ok": true, "hits":
  []}` contract, no error).
- `sources_only: true` + the LLM endpoint is down → the
  response is still returned (the LLM is never called; the
  `retrieval.llm_rerank` knob is not consulted — a
  source-grounded hit is a *vector* hit, not an LLM hit).
- `sources_only: true` on a source that has no `page` field
  (e.g. a `hermes` session) → `page: null` in the hit (the
  017 `page` field is `int | null`; the `null` is the
  documented "no page" sentinel).
- NFR-1 (dedup) holds: the Qdrant query is *unchanged*
  (same index, same `limit`, same content-addressed point
  ids); only the *response* shape is altered (no LLM
  re-ordering).
- NFR-13: no host paths in the response; only the caller-
  supplied `source` string is echoed.

## Functional Requirements

- **FR-1**: Add a `sources_only: bool = false` parameter to
  `digital_twins/mcp/dispatch.py::kb_search`'s inputSchema
  (007's `{"query": string (required), "limit": int (
  optional), "agent_kind": string (optional)}` gains one
  optional boolean; the 007 tool body is *unchanged* for
  the default `sources_only: false` case — byte-for-byte
  unchanged).
- **FR-2**: The `kb_search` body in `dispatch.py`: when
  `sources_only: true`, *skip* the LLM rerank pass (the
  `retrieval.llm_rerank` knob is not consulted — no LLM
  call is made). The raw Qdrant hit list (the
  `search_documents` result, pre-rerank) is returned as-is,
  each hit tagged with `source`, `page`, `key`, and the
  extracted chunk text (the 007 hit contract, just without
  the LLM re-ordering).
- **FR-3**: Add a `sources_only: bool = false` query
  parameter to `GET /api/v1/search` (015's HTTP layer):
  when true, the endpoint returns the raw Qdrant hit list
  (no LLM rerank); when false / omitted, the endpoint
  returns the LLM-reranked list (the 007/015 default).
- **FR-4**: No new LLM prompt. No new LLM call. The
  `retrieval.llm_rerank` knob is *not consulted* when
  `sources_only: true` — the LLM is never invoked for a
  source-grounded hit (a source-grounded hit is a *vector*
  hit, not an LLM hit).
- **FR-5**: No new Qdrant index. No new Qdrant call. The
  Qdrant query (the `search_documents` call) is *unchanged*
  (same index, same `limit`, same content-addressed point
  ids — NFR-1 dedup holds).
- **FR-6**: No change to the chunker / embedder — the
  source-grounded hit is a *response-shape* change, not an
  *extraction* change.
- **FR-7**: Test-First. Unit tests use a mock Qdrant
  (the existing `kb_search` test fixture) and assert:
  - `kb_search` with `sources_only: true` → the mock LLM
    endpoint is *not* called (0 calls recorded); the
    response is the raw Qdrant hit list (each hit carries
    `source`, `page`, `key`).
  - `kb_search` with `sources_only: false` (default) → the
    mock LLM endpoint *is* called (1 call); the response is
    the LLM-reranked list (byte-for-byte unchanged from
    007).
  - `kb_search` with `sources_only: true` + no matching
    chunks → `{"ok": true, "hits": []}` (no error).
  - `GET /api/v1/search` with `?sources_only=true` → the
    same contract as the MCP tool (the two surfaces are
    equivalent).
  - `tests/unit/test_knob_docs.py` green.

## Non-goals

- No new LLM prompt, no new LLM call — the source-grounded
  path is *vector-only* (the LLM is not involved when
  `sources_only: true`).
- No change to the Qdrant index (same index, same `limit`,
  same content-addressed point ids — NFR-1 dedup holds).
- No change to 007's `kb_chat` (the source-grounded flag is
  on `kb_search` only; `kb_chat` is a *generative* tool and
  has no "raw hit list" to return).
- No new MCP tool surface — the `sources_only` flag is on
  the *existing* `kb_search` tool (not a new `kb_search_
  sources_only` tool).
- No change to the 007 ACL gate (the `reader` role is
  unchanged; `sources_only` is a *query* parameter, not a
  *permission* parameter).
- No host paths in the response (NFR-13) — only the caller-
  supplied `source` string is echoed.

## Success Criteria

- `kb_search` with `sources_only: true` + `retrieval.llm_
  rerank: true` (the default) → the LLM endpoint is *not*
  called (0 calls recorded in the mock); the response is
  the raw Qdrant hit list.
- `kb_search` with `sources_only: false` (default) →
  byte-for-byte unchanged from 007 (the LLM rerank runs,
  the response is the LLM-reranked list).
- `kb_search` with `sources_only: true` + no matching chunks
  → `{"ok": true, "hits": []}` (no error).
- NFR-1 holds: the Qdrant query is *unchanged* (same
  index, same `limit`, same content-addressed point ids).
- `tests/unit/test_knob_docs.py` green.

## Key Entities

- `kb_search` inputSchema — gains a `sources_only: bool
  (optional, default false)` parameter.
- `GET /api/v1/search` — gains a `?sources_only=true` query
  parameter.
- `retrieval.llm_rerank` (existing 007 knob) — *not
  consulted* when `sources_only: true` (no LLM call).
- No new LLM prompt. No new Qdrant index. No new MCP tool.
  No new config knob.

## Assumptions

- The "raw Qdrant hit list" is the `search_documents` result
  *before* the LLM rerank pass (the existing 007
  `search_documents` call; no new call is made when
  `sources_only: true`).
- The `retrieval.llm_rerank` knob is *not consulted* when
  `sources_only: true` — the LLM is never invoked for a
  source-grounded hit (a source-grounded hit is a *vector*
  hit, not an LLM hit).
- The `sources_only` flag is a *response-shape* parameter
  (the LLM re-ordering is skipped, the Qdrant query is
  unchanged) — it does not alter the *extraction* or the
  *dedup* (NFR-1 holds).

## Open questions (max 3)

- [NEEDS CLARIFICATION: should `sources_only: true` *also*
  disable the 007 LLM *answer* pass (the `kb_chat`
  generation), or is the `kb_search` hit-list skip
  sufficient for v1?] — default: the `kb_search` hit-list
  skip is sufficient for v1 (a `kb_chat` source-grounded
  answer is a separate, larger feature — v1.1).
- [NEEDS CLARIFICATION: should `GET /api/v1/search` with
  `?sources_only=true` also accept a `limit` query
  parameter (to match the MCP tool's `limit`)?] — default:
  yes, a `limit` query parameter (the 015 HTTP layer
  already accepts `limit`; the `sources_only` parameter is
  an *addition* to the same query string).

## Semver

MINOR. New optional query parameter on an *existing* tool
(`kb_search`) + an *existing* endpoint
(`GET /api/v1/search`). When `sources_only` is omitted /
`false`, the response is byte-for-byte unchanged from 007 /
015. No new config knob, no new LLM call, no new Qdrant
index — NFR-1 dedup holds.
