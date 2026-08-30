# Implementation Plan: Real MCP KB Tools (007)

**Input**: spec at `specs/007-kb-search-chat/spec.md`; the 004 MCP contracts
(`registry.py` MCPContext + tool declarations, `dispatch.py`
TOOL_BODIES/dispatch), the 006 web patterns (`web/app.py`), and 001–005's
config/embedding/pipeline/health layers as the compatibility baseline.

## Constitution Compliance

- **III (Test-First, NON-NEGOTIABLE)**: every behavior in tasks.md has a RED
  test committed and failing before its GREEN implementation (each task
  names its RED file + verify command).
- **IV (knob lock-step)**: **vacuously satisfied — 007 adds ZERO knobs.**
  It reuses `qdrant.*`, `llm.*`, `embedding.*`, `sources.*` (all present
  since 001/003) through the config layer. No `config/knobs.py`,
  `config.example.yml`, `.env.example`, or `docs/configuration.md` change is
  needed; `test_knob_docs.py` must simply stay green (T021).
- **VI (migrate on open)**: **N/A — no schema changes.** `audit_runs` and
  the payload owner-stamp already exist (001/003); `kb_ingest` writes
  through the existing `run_pipeline` audit path.
- **NFR-13 (portability)**: no host paths, usernames, or install locations
  in any new or modified shipped code (all config access goes through
  `config.schema.get`/`loader.load`; no `~/.` or absolute paths).
  `tests/integration/test_portability.py` stays green — 007 adds no shipped
  files at all, so no `SHIPPED`/`SHIPPED_NON_PY` update is needed (T020).

## Technical Approach

### MCPContext.config (007-R6, T001–T004)

- `registry.py`: add `config: Any = None` to `MCPContext` (dataclass: the
  new field must be last — after `agent_kind` — so existing
  4-argument positional construction keeps working). Docstring: "The
  loaded config dict, populated by both transports (stdio:
  `config.loader.load()`; http: the `config` kwarg on
  `build_handler`/`serve`/`main`); `None` is tolerated at construction but
  every KB tool body fails closed when it is `None` (007-R6e)."
- **stdio** (`stdio.py`): `serve(...)` and `main(...)` gain a
  `config: Any = None` kwarg; when `None`, `main` loads it via
  `digital_twins.config.loader.load()`; both thread it into the
  `MCPContext(config=...)` at the construction site (line ~151).
- **http** (`http.py`): `_build_handler_cls`, `build_handler`, `serve`, and
  `main` each gain `config: Any = None`; when `None` the transport loads
  from cwd via `loader.load()`; the closure threads it into
  `MCPContext(config=...)` (line ~124).
- **CLI** (`cli.py::serve_mcp`, line 571): it already loads `cfg`
  (line 596); pass `cfg` explicitly into `stdio.main(config=cfg)` and
  `mcp_http.main(config=cfg, ...)` so the CLI path never double-loads.

### kb_search body (007-R1, T010–T011)

- New `_kb_search_body(ctx, args)` in `dispatch.py`, mirroring
  `web/app.py::_handle_kb_search` (lines 555–607):
  1. `ctx.config is None` → `config_not_loaded` (fail-closed guard).
  2. Validate `query` (blank/missing/non-str → `bad_request "query must be
     a non-empty string"`); clamp `limit` (default 5, `max(1, min(limit,
     100))`).
  3. `owner_tag = owner_tag_for(ctx.caller_email)`.
  4. Build a Qdrant client from `get(cfg, "qdrant.url")` /
     `qdrant.api_key` (the 006 `_qdrant_client` pattern, lines 383–412;
     unconfigured or construction/transport failure → `QdrantUnavailable`
     → `qdrant_unavailable` + the exact 006 503 remediation string:
     "check qdrant.url (env: KB_QDRANT__URL) points at a live Qdrant
     host:port, and that the collection exists").
  5. Embed the query **once per call**: the heavy
     `sentence_transformers` import is deferred inside the embed call
     (`ingest.embedding.load_embedder` already defers it — `load_embedder`
     at `ingest/embedding.py:43`); call
     `load_embedder(get(cfg, "embedding.model") or DEFAULT_MODEL,
     get(cfg, "embedding.device") or "auto")` with a lazy pool so the model
     loads **once per process/call-site, not per request** (006's
     `_pooled_embedder` pattern, lines 424–446, adapted to a module-level
     cache key). A load failure → `embedding_unavailable` with a
     remediation naming `embedding.model` (007-R1e — a distinct code, the
     MCP analog of 006's "never a traceback" rule).
  6. `client.query_points(QDRANT_COLLECTION, query=<vec>,
     query_filter=Filter(must=[FieldCondition(key="owner_tag",
     match=MatchValue(value=owner_tag))]), limit=limit, with_payload=True)`
     — the 006 filter verbatim; map rows to
     `{score, source_url, text, source, chunk_index}`, sort descending,
     return `{"ok": True, "results": [...], "count": len(rows)}`.
- **Seams for testing**: the Qdrant client and the embedder are resolved
  through small module-level indirections (e.g. `_resolve_qdrant_client(cfg)`,
  `_embed_query(cfg, text)`) that the RED tests monkeypatch — the same
  "seed a fake there as data" approach 006's tests use (a constructor
  monkeypatch would not fire while the body still fails).
- Wire into `TOOL_BODIES` (T011): replace the `kb_search` lambda.

### kb_chat body (007-R2, T012–T013)

- New `_kb_chat_body(ctx, args)` mirroring `web/app.py::_handle_chat`
  (lines 799–871): fail-closed guard → validate `query` (blank/missing →
  `bad_request "query must be a non-empty string"`, before any config
  read) → read `get(cfg, "llm.endpoint")` / `get(cfg, "llm.model")`
  (the only knob access; the read makes the body decision-ready for the
  follow-up slice) → return the 501-surface result
  `{"ok": False, "error": {"code": "not_implemented", "remediation":
  "set llm.endpoint / llm.model to enable chat (007 ships the surface
  only; follow-up slice fills generation)"}}` — **regardless** of whether
  the knobs are set (007 ships the surface; the 006 handler returns the
  same 501 in both branches). No LLM call, no embedding call, no Qdrant
  call, no network call. Wire into `TOOL_BODIES` (T013).

### kb_ingest body (007-R3, T014–T015)

- New `_kb_ingest_body(ctx, args)` mirroring
  `web/app.py::_handle_ingest_run` (lines 611–746), in the same order:
  1. Fail-closed guard (`config_not_loaded`).
  2. **Capability gate FIRST**:
     `require_capability(ctx.caller_role, "trigger_run", "trigger a run")`
     → refusal → `{"ok": False, "error": {"code": "permission_denied",
     "message": str(exc)}}`; **no audit row** (the gate runs before any
     pipeline work; 007-R3b).
  3. **Source validation** (007-R3c, the exact 006 messages): `source`
     omitted/`"all"` → all enabled sources; name not in
     `cfg["sources"]` → `bad_request "unknown source '<name>'"`; in config
     but `enabled` false → `bad_request "source '<name>' is not enabled"`;
     nothing enabled → `bad_request "no sources enabled"`.
  4. **Pipeline hand-off** (007-R3d, R4): `run_pipeline(merged_cfg,
     ctx.db, qdrant_factory, embedder, source_names=source_names,
     trigger="mcp", scheduled_by=ctx.caller_email, owner=ctx.caller_email)`
     — `merged_cfg` = 006's `_merge_defaults(config)` equivalent (schema
     defaults merged over the runtime config so the pipeline has the
     chunking/embedding knobs); the qdrant factory + lazy pooled embedder
     reuse the same helpers as `_kb_search_body` (shared `_resolve_qdrant`
     / pooled-embedder module state in `dispatch.py`). The audit row is
     written **by `run_pipeline` itself** (`start_audit_run`/
     `finish_audit_run`) with `trigger="mcp"`; `agent_kind` is recorded on
     the row via the existing `agent_kind` pattern (`kb_schedule_run`
     writes it into the audit record — the same
     `per_source_counts`-adjacent field 004 established, BR-11.5.3).
  5. `PrerequisiteError` → `{"code": "prerequisite_missing", "message":
     "source '<name>': missing prerequisite(s): ..."}` (the pipeline has
     already audited `failed` — do not re-audit); `UnknownSourceError` →
     the `unknown source` message; any other exception →
     `{"code": "internal_error", ...}` (never a traceback).
  6. Success → `{"ok": True, "run_id", "status", "counts", "points"}`
     (the `RunSummary` fields, same as 006 web's 200).
- `run_pipeline`'s exact signature (verified at the call site,
  `ingest/pipeline.py:105`): `run_pipeline(cfg, db, qdrant, embedder=None,
  neo4j=None, source_names=None, max_items=None, dry_run=False,
  trigger="manual", scheduled_by="system", owner=None)` — 007 passes
  positional `(merged_cfg, ctx.db, qdrant_factory, embedder)` +
  `source_names`/`trigger`/`scheduled_by`/`owner` kwargs, `neo4j` left at
  its `None` default (Qdrant-only, out of scope).
- Wire into `TOOL_BODIES` (T015).

### kb_health body (007-R4, T016–T017)

- New `_kb_health_body(ctx, args)`: fail-closed guard, then
  `checks = health.run_health_checks(ctx.config)` and map each
  `HealthResult` to `{endpoint, ok, detail, remediation}` → `{"ok": True,
  "checks": [...]}`. No I/O beyond the checks themselves (they touch
  qdrant/neo4j/llm by design; the body adds nothing). Resolved at call time
  via a module attribute so tests can monkeypatch
  `dispatch._health_mod.run_health_checks`. Wire into `TOOL_BODIES` (T017).

### Registry upgrades (007-R5, T005–T009)

- New schema builders in `registry.py` next to the 004 builders:
  `_kb_search_schema()` (`query` string required; `limit` int default 5
  max 100; `agent_kind` via the existing `_agent_kind_prop()`),
  `_kb_chat_schema()` (`query` required; `agent_kind`),
  `_kb_ingest_schema()` (`source` optional string; `agent_kind`),
  `_kb_health_schema()` (`agent_kind` only).
- `build_tool_registry()`: replace the four `{"...stub..."}` entries with
  the new builders + descriptions that state the real behavior and error
  codes (e.g. kb_search: "Owner-scoped vector search over the personal_kb
  collection. Args: query (required), limit (default 5, max 100). Errors:
  bad_request, qdrant_unavailable, embedding_unavailable."). T018 deletes
  `_stub_schema()` — it has no remaining callers after T009.

## File Map

| File | Why |
|---|---|
| `digital_twins/mcp/registry.py` | `MCPContext.config` field + docstring (T001); four real schema builders (T005–T008); stub declarations → real (T009); delete `_stub_schema()` (T018). |
| `digital_twins/mcp/stdio.py` | `serve`/`main` gain `config` kwarg, default `loader.load()`, threaded into `MCPContext` (T002). |
| `digital_twins/mcp/http.py` | `_build_handler_cls`/`build_handler`/`serve`/`main` gain `config` kwarg, threaded into `MCPContext` (T003). |
| `digital_twins/cli.py` | `serve_mcp` passes its loaded `cfg` into both transports (T004). |
| `digital_twins/mcp/dispatch.py` | the four `_kb_*_body` functions + shared qdrant/embedding helpers (T010/T012/T014/T016) and `TOOL_BODIES` wiring (T011/T013/T015/T017). |
| `tests/unit/test_mcp_kb_search.py` | NEW — registry schema shape; owner-scoped filter; query_points call args; blank query → bad_request; limit default/cap; qdrant_unavailable + embedding_unavailable shapes. |
| `tests/unit/test_mcp_kb_chat.py` | NEW — 501-surface shape; no LLM/embedding/qdrant call; llm.endpoint/model read decision. |
| `tests/unit/test_mcp_kb_ingest.py` | NEW — capability gate (reader → permission_denied, no audit row); source validation (unknown/disabled/none-enabled); run_pipeline hand-off with trigger="mcp" + owner + scheduled_by + agent_kind. |
| `tests/unit/test_mcp_kb_health.py` | NEW — health checks wrapped; ok shape; config=None → config_not_loaded; no I/O beyond run_health_checks. |
| `tests/integration/test_mcp_kb_tools.py` | NEW — e2e: service-token auth → dispatch kb_search against a fake Qdrant client; personal-token auth → dispatch kb_ingest against a fake run_pipeline; audit row has trigger="mcp" + agent_kind; both transports populate `MCPContext.config`. |
| `digital_twins/__init__.py` | version 0.6.0 → 0.7.0 (T023, merge-time task, in-branch). |
| `CHANGELOG.md` | 0.7.0 section, Keep-a-Changelog style, host-neutral (T023). |

No new shipped files (007 adds **zero** shipped artifacts — all changes are
in existing `digital_twins/` modules), so `test_portability.py`'s
`SHIPPED`/`SHIPPED_NON_PY` lists are untouched.

## Dependencies (what lands before what)

```
Phase 1 (config plumbing)
  T001 MCPContext.config field (registry.py)          <- nothing
  T002 stdio transport threads config                 <- T001
  T003 http transport threads config                  <- T001
  T004 cli.serve_mcp passes cfg to both transports    <- T002, T003
Phase 2 (registry)
  T005–T008 schema builders                           <- T001 (independent of bodies)
  T009 registry declarations → real schemas          <- T005..T008
Phase 3 (bodies)
  T010/T011 kb_search RED+GREEN                       <- T001 (needs ctx.config)
  T012/T013 kb_chat RED+GREEN                         <- T001
  T014/T015 kb_ingest RED+GREEN                       <- T001
  T016/T017 kb_health RED+GREEN                       <- T001
  T018 delete _stub_schema()                          <- T009
Phase 4 (e2e + guards)
  T019 integration e2e                                <- all of Phases 1–3
  T020 portability guard re-run                       <- all code done
  T021 knob-docs guard re-run                         <- all code done
  T022 full-suite green                               <- all
Phase 5 (release)
  T023 version bump + CHANGELOG                       <- T022 (merge-time)
  T024 final whole-branch review (separate subagent)  <- T023
```

## Risk & Mitigation

- **Embedding model load is heavy** (sentence-transformers import + model
  download): the import is deferred inside `load_embedder` (001's design)
  and the model is pooled once per process via a lazy pool (006's
  `_pooled_embedder` pattern, adapted to a `dispatch.py` module cache). A
  load failure is the predictable `embedding_unavailable` error code, never
  a traceback — unit tests monkeypatch the embed seam so the real model is
  never loaded in tests.
- **Qdrant owner-scope must be exact** (a leaked cross-owner result is a
  data-leak bug, not a bug): the owner filter (`Filter(must=[
  FieldCondition(key="owner_tag", match=MatchValue(value=owner_tag))])`) is
  mirrored verbatim from 006's `_handle_kb_search`; the RED tests assert the
  filter object passed to `query_points` and the e2e test asserts a second
  owner's points never appear.
- **MCPContext.config back-compat** (existing positional construction in
  004 tests/transport): the field is appended **last** with a default of
  `None`, so every existing 4-arg `MCPContext(...)` call keeps working; the
  fail-closed `config_not_loaded` guard makes a `None` config a clean,
  named error instead of an `AttributeError` — covered by a dedicated RED
  test.
- **kb_ingest double-audit**: the audit row is written by `run_pipeline`
  (`start_audit_run`/`finish_audit_run`), never by the MCP body; the body
  only maps exceptions. On a `PrerequisiteError` the pipeline's `failed`
  row already stands — the body surfaces it without writing a second row.
  The RED test asserts exactly one audit row per attempted run.

## Testing strategy (Test-First, constitution III)

RED-before-GREEN per task; the RED tests are committed and failing before
the GREEN code. The checks map to the spec's acceptance criteria:

- **SC-001/SC-002** → `tests/unit/test_mcp_kb_search.py` (owner filter,
  limit clamp, exact error strings; fake Qdrant client + fake embedder via
  the module seams) + the e2e (T019).
- **SC-003** → `tests/unit/test_mcp_kb_chat.py` (501 shape; spies assert no
  qdrant/embedding/network activity) + e2e.
- **SC-004** → `tests/unit/test_mcp_kb_ingest.py` (gate-first, no audit row
  on refusal; source-validation messages; fake `run_pipeline` capturing
  `trigger="mcp"`/`owner`/`scheduled_by`) + e2e (real audit row via the
  pipeline's audit functions).
- **SC-005** → `tests/unit/test_mcp_kb_health.py` (wrapped `run_health_checks`,
  per-endpoint dict shape; `config=None` → `config_not_loaded`).
- **SC-006** → registry assertions inside `test_mcp_kb_search.py` +
  `test_mcp_kb_health.py` (one per tool: schema properties/required +
  description free of "stub").
- **SC-007** → e2e (T019): stdio + http both dispatch with non-`None`
  config; a direct `MCPContext(config=None)` dispatch fails closed.
- **SC-008** → T022 full-suite run + T020/T021 guard re-runs.
