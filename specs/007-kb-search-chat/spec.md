# Feature Specification: Real MCP KB Tools — Replace the 004 BR-10 Stubs

**Feature Branch**: `007-kb-search-chat`

**Created**: 2026-08-31

**Status**: Draft

**Input**: Controller brief `.superpowers/sdd/007-kb-search-chat/007-brief.md`
(binding scope document). 007 implements the KB read/ingest/health surface on
the MCP side of 004, closing the four BR-10 stubs that 004 declared but left
as `not_implemented_yet`: `kb_search`, `kb_chat`, `kb_ingest`, `kb_health`.

## Summary

Feature 007 replaces the four BR-10 stub tool bodies in `digital_twins/mcp/`
(dispatch `TOOL_BODIES`, registry `_stub_schema` declarations) with real
implementations: `kb_search` (owner-scoped Qdrant vector search, the MCP
mirror of 006's `POST /api/kb/search`), `kb_chat` (the 006 501 surface,
auth + scoping + validation, no generation — BR-11.5.2's "an agent with a
valid token may `kb_search`, `kb_chat`"), `kb_ingest` (`run_pipeline`
hand-off with `trigger="mcp"`, making MCP a true second ingestion surface,
BR-11.5.2/BR-4.7 and the one-record-not-N acceptance criterion), and
`kb_health` (a thin wrap of `health.run_health_checks`). The registry gains
full `inputSchema`s for all four tools, `MCPContext` gains a `config: Any`
field that both transports (stdio, http) populate, and every tool body
fails closed when `config` is `None`. Auth is unchanged (BR-11.5.2) and
`kb_ingest` audits with `trigger="mcp"` + `agent_kind` (BR-11.5.3).

## Scope

**In scope** (per 007-brief.md "In scope"):

- The four real tool bodies in `digital_twins/mcp/dispatch.py` replacing the
  stub lambdas at lines 573–604.
- Registry upgrades in `digital_twins/mcp/registry.py`: full `inputSchema`
  builders for the four tools, replacing `_stub_schema()` at lines 289–316,
  and updated `description` strings that no longer say "stub".
- `MCPContext.config` (the loaded config dict, `None`-tolerant, fail-closed
  consumers) and both transports populating it:
  `stdio.py::main/serve`, `http.py::build_handler/serve/main`, and
  `cli.py::serve_mcp` threading its already-loaded `cfg` into both.
- The RED test files named in "Test surface" below + one integration
  e2e file; the standing guards (`test_portability.py`,
  `test_knob_docs.py`) stay green.

**Out of scope** (per 007-brief.md "Out of scope"):

- No new config knobs (007 reuses `qdrant.*`, `llm.*`, `embedding.*`,
  `sources.*` — all present since 001/003).
- No real LLM generation for `kb_chat` (surface only; a follow-up slice fills
  generation when `llm.endpoint`/`llm.model` are configured — same ruling as
  006 R8/C-1).
- No ACL changes beyond `accounts.ROLE_CAPS` (reader searches/chats;
  scheduler/admin ingest; health is open to any authenticated user; Q9's
  per-schedule ACL placeholder stays untouched — BR-11.5.5).
- No transport protocol changes (NDJSON stdio + JSON-over-HTTP stay as-is;
  BR-11.5.4 is already satisfied by 004 and is not re-touched).
- No Neo4j access (007 is Qdrant-only, matching 006's KB read surface).
- No web/UI changes (006 owns those surfaces) and no migrations/schema
  changes.

## Requirements

All error shapes follow the 004/006 convention: on success
`{"ok": True, ...}`; on failure `{"ok": False, "error": {"code":
<snake_case_code>, "message"|"remediation": <hint>}}` — no tracebacks leak,
no 500-equivalent on predictable failures (007-brief.md "Global
constraints").

### kb_search (007-R1)

- **007-R1a**: `kb_search` SHALL accept `{query: string (required),
  limit?: int (default 5, cap 100), agent_kind?: string}`. Blank or missing
  `query` SHALL return `{"ok": False, "error": {"code": "bad_request",
  "message": "query must be a non-empty string"}}` — 006's exact 400 message.
- **007-R1b**: `kb_search` SHALL embed the query with the config-pinned
  model (`embedding.model`/`embedding.device`, reusing
  `ingest.embedding.load_embedder` — R4: no parallel embedding path) and run
  an owner-scoped `query_points` on the `personal_kb` collection
  (`health.QDRANT_COLLECTION`) using the caller's `owner_tag`
  (`accounts.owner_tag_for(ctx.caller_email)`), mirroring
  `web/app.py::_handle_kb_search`'s `Filter`/`FieldCondition`/`MatchValue`
  filter verbatim.
- **007-R1c**: On success `kb_search` SHALL return `{"ok": True, "results":
  [{score, source_url, text, source, chunk_index}], "count": N}` with
  results sorted by descending score, `count = len(results)` ≤ `limit`.
- **007-R1d**: Qdrant unconfigured/unreachable or a failed `query_points`
  call SHALL return `{"ok": False, "error": {"code": "qdrant_unavailable",
  "remediation": "check qdrant.url (env: KB_QDRANT__URL) points at a live
  Qdrant host:port, and that the collection exists"}}` — 006's 503 hint in
  the MCP error shape; never a traceback.
- **007-R1e**: A failure to load the embedding model SHALL return a
  predictable `{"ok": False, "error": {"code": "embedding_unavailable",
  "remediation": ...}}` (naming `embedding.model`) — the MCP analog of 006's
  "never a traceback" rule, with a code distinct from `qdrant_unavailable`.

### kb_chat (007-R2)

- **007-R2a**: `kb_chat` SHALL accept `{query: string (required),
  agent_kind?: string}`. Blank or missing `query` SHALL return
  `{"ok": False, "error": {"code": "bad_request", "message": "query must be
  a non-empty string"}}` (validation before any config read, same order as
  006's handler).
- **007-R2b**: `kb_chat` SHALL read `llm.endpoint`/`llm.model` from the
  config layer (the only knob access the body performs) and return the
  501-surface result `{"ok": False, "error": {"code": "not_implemented",
  "remediation": "set llm.endpoint / llm.model to enable chat (007 ships
  the surface only; follow-up slice fills generation)"}}` — 006's 501
  surface in the MCP shape. The read makes the body decision-ready: the
  follow-up slice branches on exactly this state.
- **007-R2c**: `kb_chat` SHALL make NO LLM call, NO embedding call, NO
  Qdrant call, and NO network call — the surface (auth + scoping +
  validation + error shape) is the entire 007 delivery, mirroring 004's
  BR-10 not_implemented pattern and 006's C-1/R8.

### kb_ingest (007-R3)

- **007-R3a**: `kb_ingest` SHALL accept `{source?: string, agent_kind?:
  string}`; `source` omitted/`"all"` runs all enabled sources (mirrors 006
  web + `run --once`).
- **007-R3b**: The `trigger_run` capability gate SHALL run FIRST, before any
  pipeline work: `require_capability(ctx.caller_role, "trigger_run",
  "trigger a run")` → `{"ok": False, "error": {"code":
  "permission_denied", "message": ...}}` on refusal, and **no audit row is
  written on refusal**.
- **007-R3c**: Source validation SHALL mirror `web/app.py::_handle_ingest_run`:
  unknown source → `{"code": "bad_request", "message": "unknown source
  '<name>'"}`; disabled source → `"source '<name>' is not enabled"`; none
  enabled → `"no sources enabled"` (all `bad_request`).
- **007-R3d**: `kb_ingest` SHALL hand off to
  `digital_twins.ingest.pipeline.run_pipeline` verbatim (R4 — the same code
  path as `run --once` and 006 web) with `trigger="mcp"`,
  `scheduled_by=ctx.caller_email`, `owner=ctx.caller_email`, and
  `agent_kind` recorded on the audit row (the existing `agent_kind` pattern
  from `kb_schedule_run`, BR-11.5.3). The audit row (start/finish) is
  written by `run_pipeline` itself; on a missing-prerequisite failure the
  pipeline's `failed` audit row stands and the body surfaces
  `{"code": "prerequisite_missing", "message": "source '<name>': missing
  prerequisite(s): ..."}`.
- **007-R3e**: On success `kb_ingest` SHALL return `{"ok": True, "run_id",
  "status", "counts", "points"}` — the run summary in the MCP shape.

### kb_health (007-R4)

- **007-R4a**: `kb_health` SHALL accept `{agent_kind?: string}` and wrap
  `digital_twins.health.run_health_checks(ctx.config)` verbatim, returning
  `{"ok": True, "checks": [{endpoint, ok, detail, remediation}]}` — one
  entry per endpoint (qdrant, neo4j, llm). No I/O beyond the checks
  themselves.

### Registry (007-R5)

- **007-R5a**: The registry SHALL declare full `inputSchema`s for all four
  tools as specified above (007-R1a/R2a/R3a/R4a), replacing the four
  `_stub_schema()` entries; the six 004 scheduler-tool declarations are
  unchanged.
- **007-R5b**: The four tool `description` strings SHALL describe the real
  behavior and its error codes — no "BR-10 stub … not implemented in 004"
  wording remains.

### MCPContext.config (007-R6)

- **007-R6a**: `MCPContext` SHALL gain a `config: Any` field (the loaded
  config dict; `None` allowed). The dataclass default is `None`; the docstring
  documents that both transports must populate it for the KB tools.
- **007-R6b**: The stdio transport SHALL populate it: `stdio.main` loads
  config via `digital_twins.config.loader.load()` and threads it into
  `MCPContext`.
- **007-R6c**: The http transport SHALL accept a `config` kwarg on
  `build_handler`/`serve`/`main` (default: load from cwd via the config
  layer) and thread it into `MCPContext`.
- **007-R6d**: `cli.py::serve_mcp` SHALL pass its already-loaded `cfg` into
  both transports.
- **007-R6e**: Every tool body that needs the config SHALL fail closed when
  `ctx.config is None`: `{"ok": False, "error": {"code":
  "config_not_loaded", "message": "MCPContext.config is None; the transport
  must load config before dispatch"}}` — before any other work.

### Auth + audit (007-R7)

- **007-R7a**: The MCP auth model SHALL be unchanged (BR-11.5.2): account
  session or service token via `DT_SERVICE_TOKEN`/`DT_PERSONAL_TOKEN`,
  exactly as 004's `mcp_authenticator` + stdio process-owner credential
  already provide. No new auth surface.
- **007-R7b**: A `kb_ingest` run SHALL be audited identically to
  API/UI/CLI runs (BR-11.5.3) with `trigger="mcp"` and `agent_kind`
  recorded on the row — closing the acceptance criterion "an external MCP
  agent authenticates with a token, calls `kb_search`, and — if admin —
  `kb_ingest` … the run is audited with `trigger: "mcp"` and the agent's
  identity."

## Acceptance

- **SC-001**: `kb_search` on a populated, owner-tagged collection returns
  `{"ok": True, "results": [...], "count": N}` with owner-scoped top-N in
  the exact 006 field set (`score`/`source_url`/`text`/`source`/
  `chunk_index`); a different owner's points never appear in the results.
- **SC-002**: `kb_search` with a blank/missing query returns the exact
  `bad_request "query must be a non-empty string"`; with Qdrant
  unconfigured/unreachable it returns `qdrant_unavailable` + the 006
  remediation string; with a broken embedding model it returns
  `embedding_unavailable` — no traceback on any of the three.
- **SC-003**: `kb_chat` returns the clean `not_implemented` 501-surface
  result with the remediation hint naming `llm.endpoint`/`llm.model`, with
  no LLM/embedding/Qdrant/network call — asserted by the e2e test via
  spies on the network- and client-facing seams.
- **SC-004**: `kb_ingest` with a service-token caller and an enabled source
  runs `run_pipeline` with `trigger="mcp"`, `scheduled_by=<caller>`,
  `owner=<caller>`; the `audit_runs` row carries `trigger="mcp"` and
  `agent_kind`; a reader-role caller gets `permission_denied` with **no**
  audit row written; unknown/disabled/none-enabled sources get the exact
  006 `bad_request` messages.
- **SC-005**: `kb_health` returns `{"ok": True, "checks": [...]}` with one
  entry per endpoint in the `HealthResult` field shape, against
  `run_health_checks` — no other I/O.
- **SC-006**: `build_tool_registry()` returns full `inputSchema`s for all
  four KB tools (query/limit/source/agent_kind properties with required
  lists) and no description contains "stub" or "not implemented in 004".
- **SC-007**: Both transports populate `MCPContext.config` — a stdio call
  and an http call each dispatch the KB tools with a non-`None` config;
  dispatching a KB tool with `MCPContext(config=None)` fails closed with
  `config_not_loaded`.
- **SC-008**: Full suite green on the worktree (baseline 762 at main @
  7fd350b; 007 adds ~40–60 new tests); `tests/integration/test_portability.py`
  and `tests/unit/test_knob_docs.py` stay green.

## Non-Goals

- No LLM/RAG generation (`kb_chat` is surface-only; follow-up slice).
- No Neo4j access (Qdrant-only, matching 006's KB read surface).
- No new config knobs (zero knob changes — constitution IV vacuous).
- No ACL changes (Q9 placeholder stays; BR-11.5.5 follow-up).
- No web/UI or HTTP-API changes (006 owns those surfaces).
- No migrations or schema changes (constitution VI, N/A).

## Assumptions

- 001–006 are complete at v0.6.0 on main @ 7fd350b; `digital_twins.mcp`
  (auth, registry, acl, dispatch, stdio, http), `digital_twins.web.app`,
  `digital_twins.ingest.pipeline`, `digital_twins.health`, and the config
  layer all exist and are reused, not re-implemented.
- The user has their own Qdrant/Neo4j/LLM endpoints configured
  (`qdrant.url`, etc.); 007 reads them through the config layer at runtime
  (no host defaults — NFR-13).
- Version stays `0.6.0` during the SDD loop; the bump to `0.7.0` +
  CHANGELOG entry is the final in-branch task (T023) and the tag happens
  only after the merge gate (007-brief.md controller rulings R8/R15).
- All owner decisions Q1–Q10 are locked (requirement.md §5); Q9's ACL
  placeholder governs the access model and is not re-litigated.

## Rulings (carried from 006 span, honored — not re-opened)

- **R1**: skip the `clarify` phase (specify → plan → tasks); the owner locked
  scope in the brief + BR-11.5.
- **R4-style**: reuse the existing pipeline/config/embedding layers; no
  parallel logic — `kb_search` reuses 006's Qdrant + `ingest.embedding`
  pattern verbatim; `kb_ingest` reuses `run_pipeline` verbatim; `kb_health`
  reuses `health.run_health_checks` verbatim.
- **R9**: the `workflow` tool is unusable for SDD orchestration; direct
  `subagent` dispatch only.
- **R8/R15**: version bump + CHANGELOG + tag `v0.7.0` happen only at merge
  time; in-loop the version stays `0.6.0`.
