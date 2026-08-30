# Tasks: Real MCP KB Tools — Replace the 004 BR-10 Stubs (007)

**Input**: Design documents from `specs/007-kb-search-chat/` (spec.md,
plan.md); the binding brief at
`.superpowers/sdd/007-kb-search-chat/007-brief.md` (scope, rulings R1/R4/
R8/R9/R15, test surface, success criteria, file pointers).

**Prerequisites**: spec.md, plan.md, 007-brief.md (all present).

**Tests**: Included — constitution III (Test-First, NON-NEGOTIABLE): each
task's RED test is committed and failing **before** its GREEN
implementation. "RED" tasks create/extend a test file + record the failing
run; "GREEN" tasks make it pass.

**Organization**: tasks are single committable units, grouped by phase;
each names its RED test pointer, GREEN implementation pointer, and a
verify command. `007-R*` IDs refer to `specs/007-kb-search-chat/spec.md`
requirements.

## Path Conventions

Single project: `digital_twins/` and `tests/` at repository root. All work
happens in the worktree `.worktrees/007-kb-search-chat` (branch
`007-kb-search-chat`). No file outside `specs/007-kb-search-chat/` and the
files named in the plan is created or modified.

---

## Phase 1: MCPContext.config Plumbing (007-R6)

**Purpose**: `MCPContext` gains `config: Any = None`; both transports
populate it; the CLI threads its loaded `cfg`. No tool-body behavior
changes yet.

- [ ] T001 RED+GREEN: `MCPContext.config` field — RED: create
  `tests/unit/test_mcp_config_plumbing.py` asserting
  `MCPContext(db, "a@b", "reader", "stdio")` (4-arg positional, the 004
  call shape) still constructs with `config is None`, and
  `MCPContext(..., config={})` carries the dict (plus the `config_not_loaded`
  fail-closed behavior asserted in T010/T012/T014/T016). Verify:
  `pytest tests/unit/test_mcp_config_plumbing.py` RED (no `config` field
  yet). GREEN: `digital_twins/mcp/registry.py` — add `config: Any = None`
  as the **last** dataclass field (after `agent_kind`, so positional 004
  calls keep working) and extend the docstring: both transports must
  populate it; KB tool bodies fail closed when it is `None` (007-R6a/R6e).
  Verify: the same command GREEN.

- [ ] T002 RED+GREEN: stdio transport threads config — RED: extend
  `test_mcp_config_plumbing.py`: `stdio.main`/`stdio.serve` accept a
  `config` kwarg; when omitted, `main` loads it via
  `digital_twins.config.loader.load()` (monkeypatch the loader); the
  constructed `MCPContext` carries `config` as a dict (not `None`)
  (`stdio.py` lines ~151–156, ~187–189). Verify:
  `pytest tests/unit/test_mcp_config_plumbing.py` RED. GREEN:
  `digital_twins/mcp/stdio.py` — `serve(...)` and `main(...)` gain
  `config: Any = None`; `main` loads via `loader.load()` when `config is
  None`; thread `config=config` into the `MCPContext(...)` construction.
  Verify: GREEN.

- [ ] T003 RED+GREEN: http transport threads config — RED: extend
  `test_mcp_config_plumbing.py`: `http.build_handler`/`http.serve`/
  `http.main`/`_build_handler_cls` accept a `config` kwarg (default: load
  from cwd via the config layer, monkeypatchable); the handler's
  `MCPContext` (line ~124) carries the config dict. Verify:
  `pytest tests/unit/test_mcp_config_plumbing.py` RED. GREEN:
  `digital_twins/mcp/http.py` — add the `config: Any = None` kwarg through
  all four functions; when `None`, load via `digital_twins.config.loader.
  load()`; thread it into `MCPContext(config=...)`. Verify: GREEN.

- [ ] T004 RED+GREEN: `cli.serve_mcp` passes loaded cfg to both transports
  — RED: extend `test_mcp_config_plumbing.py`: with a tmp state dir +
  config, the `serve-mcp` click command (run in a subprocess with piped
  stdio, or the test seam it exposes) passes the **already-loaded** `cfg`
  (line 596's `load()`) into `stdio.main(config=cfg)` and
  `mcp_http.main(config=cfg, ...)` — assert via monkeypatched
  `stdio.main`/`mcp_http.main` that the `config` kwarg is exactly the dict
  returned by the CLI's own `load()` call (no second load). Verify:
  `pytest tests/unit/test_mcp_config_plumbing.py` RED. GREEN:
  `digital_twins/cli.py::serve_mcp` (line 571) — pass `config=cfg` into
  both transport calls. Verify: GREEN.

---

## Phase 2: Registry Upgrades (007-R5)

**Purpose**: full `inputSchema`s for the four tools; descriptions no longer
say "stub"; the `_stub_schema()` helper deleted.

- [x] T005 RED: `_kb_search_schema()` builder — RED: extend
  `tests/unit/test_mcp_config_plumbing.py` (or a new
  `tests/unit/test_mcp_registry_schemas.py` — create it):
  `registry._kb_search_schema()` returns an object-schema with
  `query` (string, in `required`), `limit` (integer, default 5,
  description notes max 100), `agent_kind` (string, the existing
  `_agent_kind_prop()` description). Verify:
  `pytest tests/unit/test_mcp_registry_schemas.py` RED. GREEN:
  `digital_twins/mcp/registry.py` — add the builder (007-R1a). Verify:
  GREEN.

- [x] T006 RED: `_kb_chat_schema()` builder — RED: same file:
  `_kb_chat_schema()` has `query` (string, required) + `agent_kind`.
  Verify: `pytest tests/unit/test_mcp_registry_schemas.py` RED. GREEN:
  add the builder (007-R2a). Verify: GREEN.

- [x] T007 RED: `_kb_ingest_schema()` builder — RED: same file:
  `_kb_ingest_schema()` has `source` (string, **optional** — omitted/`"all"`
  runs all enabled, mirroring 006 web + `run --once`) + `agent_kind`;
  `required: []`. Verify: RED. GREEN: add the builder (007-R3a). Verify:
  GREEN.

- [x] T008 RED: `_kb_health_schema()` builder — RED: same file:
  `_kb_health_schema()` has only `agent_kind`; `required: []`. Verify:
  RED. GREEN: add the builder (007-R4a). Verify: GREEN.

- [x] T009 GREEN: replace the four stub declarations in
  `build_tool_registry` — extend `test_mcp_registry_schemas.py`: the
  registry's four KB entries now carry the real schemas (query/limit/source
  properties present with the right required lists) and their
  `description` strings describe the real behavior + error codes and
  contain **no** "stub" or "not implemented in 004" wording (007-R5a/R5b,
  SC-006). The six 004 scheduler declarations are byte-identical to before
  (assert one is unchanged as a canary). RED (schemas are still
  `_stub_schema`), then GREEN: `registry.py` — swap the four entries
  (lines ~289–316) to the new builders + updated descriptions. Verify:
  `pytest tests/unit/test_mcp_registry_schemas.py` GREEN.

---

## Phase 3: Tool Bodies (007-R1..R4)

**Purpose**: the four real bodies in `dispatch.py`, each RED-first.

- [ ] T010 RED: `kb_search` unit tests — create
  `tests/unit/test_mcp_kb_search.py` (007-R1, SC-001/SC-002/SC-007):
  (a) owner-scoped filter — a fake Qdrant client (seeded via the dispatch
  module's client seam) captures `query_points` args: the
  `query_filter` is exactly
  `Filter(must=[FieldCondition(key="owner_tag",
  match=MatchValue(value=owner_tag_for(caller)))])` on
  `health.QDRANT_COLLECTION`, with `limit` clamped and `with_payload=True`;
  (b) `{"ok": True, "results": [{score, source_url, text, source,
  chunk_index}], "count": N}` from fake rows, sorted descending, `count =
  len(results)`; (c) blank/missing query → `bad_request "query must be a
  non-empty string"` (exact 006 message) and **no** Qdrant call; (d) limit
  default 5 / cap 100 / non-int → default; (e) unconfigured/unreachable
  Qdrant → `qdrant_unavailable` + the exact 006 remediation string
  ("check qdrant.url (env: KB_QDRANT__URL) points at a live Qdrant
  host:port, and that the collection exists"); (f) broken embedder →
  `embedding_unavailable` (a distinct code, naming `embedding.model`);
  (g) `MCPContext(config=None)` → `config_not_loaded` before any other
  work. Embedding is faked at the dispatch module seam (never load the
  real model in tests). RED against the still-stub lambda
  (`not_implemented_yet`). Verify: `pytest tests/unit/test_mcp_kb_search.py`
  RED.

- [ ] T011 GREEN: `_kb_search_body` + shared helpers + wiring — implement
  in `digital_twins/mcp/dispatch.py` (plan "kb_search body"):
  `_kb_search_body(ctx, args)` mirroring `web/app.py::_handle_kb_search`
  (lines 555–607) — fail-closed guard → query validation → limit clamp →
  `owner_tag_for(ctx.caller_email)` → `_resolve_qdrant_client(ctx.config)`
  (006 `_qdrant_client` pattern, lines 383–412; unconfigured/construction
  failure → `qdrant_unavailable` + exact remediation) → pooled
  `_embed_query(ctx.config, text)` (`load_embedder(get(cfg,
  "embedding.model") or DEFAULT_MODEL, get(cfg, "embedding.device") or
  "auto")`, lazy pool so the model loads once, not per request; load
  failure → `embedding_unavailable`) → `query_points` with the owner filter
  verbatim → rows → `{"ok": True, "results": [...], "count": N}`. Wire
  `kb_search` into `TOOL_BODIES` (replace the lambda at line ~573). Verify:
  `pytest tests/unit/test_mcp_kb_search.py` GREEN.

- [ ] T012 RED: `kb_chat` unit tests — create
  `tests/unit/test_mcp_kb_chat.py` (007-R2, SC-003): (a) the result is
  `{"ok": False, "error": {"code": "not_implemented", "remediation":
  "set llm.endpoint / llm.model to enable chat (007 ships the surface
  only; follow-up slice fills generation)"}}` — with `llm.endpoint`/
  `llm.model` unset **and** set (007 returns the surface result either
  way, mirroring 006's two-branch handler); (b) the body performs the
  `llm.endpoint`/`llm.model` config read (decision-ready — assert via a
  spy config dict or the `config.schema.get` seam); (c) blank/missing
  query → `bad_request "query must be a non-empty string"` **before** any
  config read; (d) NO LLM call, NO embedding call, NO Qdrant call, NO
  network call (spies on the qdrant/embed seams + a network patch all
  record zero calls); (e) `config=None` → `config_not_loaded`. RED against
  the stub lambda. Verify: `pytest tests/unit/test_mcp_kb_chat.py` RED.

- [ ] T013 GREEN: `_kb_chat_body` + wiring — implement
  `_kb_chat_body(ctx, args)` in `dispatch.py` (plan "kb_chat body"):
  fail-closed guard → query validation → read
  `get(cfg, "llm.endpoint")` / `get(cfg, "llm.model")` (the only knob
  access; read makes the body decision-ready for the follow-up slice) →
  return the 501-surface result. No LLM/embedding/Qdrant/network call.
  Wire into `TOOL_BODIES` (replace the lambda at line ~581). Verify:
  `pytest tests/unit/test_mcp_kb_chat.py` GREEN.

- [ ] T014 RED: `kb_ingest` unit tests — create
  `tests/unit/test_mcp_kb_ingest.py` (007-R3, SC-004): with a fake
  `run_pipeline` (monkeypatched on the `dispatch` module attribute so the
  hand-off is captured, 006's `_pipeline_mod.run_pipeline` pattern):
  (a) **capability gate first** — `ctx.caller_role="reader"` →
  `{"ok": False, "error": {"code": "permission_denied", ...}}` naming
  `trigger_run`, and the fake `run_pipeline` records **zero** calls AND
  the DB has **no** `audit_runs` row (no audit on refusal — 007-R3b);
  (b) scheduler/admin role + enabled source → fake `run_pipeline` called
  once with positional `(merged_cfg, ctx.db, <qdrant factory>, <embedder>)`
  and `source_names=[<name>]`, `trigger="mcp"`,
  `scheduled_by=ctx.caller_email`, `owner=ctx.caller_email`
  (NO `agent_kind` kwarg — `run_pipeline`'s signature has none; verify at
  `ingest/pipeline.py:105`). `agent_kind` is recorded on the audit row via
  the 004 `_stamp_agent_kind(ctx.db, summary.run_id, ctx.agent_kind)` post-call
  pattern (007-R3d/BR-11.5.3 — mirror `_kb_schedule_run_body` step 5);
  (c) source validation (exact 006 messages, all `bad_request`,
  zero pipeline calls): unknown → `"unknown source '<name>'"`, disabled →
  `"source '<name>' is not enabled"`, `source` omitted/`"all"` with none
  enabled → `"no sources enabled"`; (d) `PrerequisiteError` →
  `prerequisite_missing` `"source '<name>': missing prerequisite(s):
  ..."` with exactly one `failed` audit row (the pipeline's own — the body
  writes no second row); (e) `UnknownSourceError` from the pipeline →
  `unknown source` message; (f) success → `{"ok": True, "run_id",
  "status", "counts", "points"}`; (g) `config=None` → `config_not_loaded`.
  RED against the stub lambda. Verify:
  `pytest tests/unit/test_mcp_kb_ingest.py` RED.

- [ ] T015 GREEN: `_kb_ingest_body` + wiring — implement
  `_kb_ingest_body(ctx, args)` in `dispatch.py` (plan "kb_ingest body"):
  mirror `web/app.py::_handle_ingest_run` (lines 611–746) order —
  fail-closed guard → `require_capability(ctx.caller_role, "trigger_run",
  "trigger a run")` (refusal → `permission_denied`, no audit row) → source
  validation → `run_pipeline(merged_cfg, ctx.db, qdrant_factory, embedder,
  source_names=..., trigger="mcp", scheduled_by=ctx.caller_email,
  owner=ctx.caller_email)` resolved at call time via the module attribute
  (monkeypatch seam), reusing Phase-3's `_resolve_qdrant` + pooled-embedder
  helpers; `neo4j` left at its default (Qdrant-only); audit row written by
  `run_pipeline` (start/finish_audit_run) with `trigger="mcp"` +
  `agent_kind`; `PrerequisiteError`/`UnknownSourceError`/other mapped per
  007-R3d. `merged_cfg` = schema defaults merged over `ctx.config` (006's
  `_merge_defaults` equivalent). Wire into `TOOL_BODIES` (replace the
  lambda at line ~589). Verify: `pytest tests/unit/test_mcp_kb_ingest.py`
  GREEN.

- [ ] T016 RED: `kb_health` unit tests — create
  `tests/unit/test_mcp_kb_health.py` (007-R4, SC-005): with a monkeypatched
  `dispatch._health_mod.run_health_checks` returning fake
  `HealthResult(endpoint, ok, detail, remediation)` entries: result is
  `{"ok": True, "checks": [{endpoint, ok, detail, remediation}]}` with one
  entry per fake (field shape preserved, order preserved); the fake is
  called **exactly once** with `ctx.config` as its sole argument (no other
  I/O — assert no qdrant/embedding/spies fired); `config=None` →
  `config_not_loaded`. RED against the stub lambda. Verify:
  `pytest tests/unit/test_mcp_kb_health.py` RED.

- [ ] T017 GREEN: `_kb_health_body` + wiring — implement
  `_kb_health_body(ctx, args)` in `dispatch.py` (plan "kb_health body"):
  fail-closed guard, then `checks = _health_mod.run_health_checks(ctx.config)`
  (module-attribute seam for the monkeypatch), map each `HealthResult` to
  `{endpoint, ok, detail, remediation}` → `{"ok": True, "checks": [...]}`.
  Wire into `TOOL_BODIES` (replace the lambda at line ~597). Verify:
  `pytest tests/unit/test_mcp_kb_health.py` GREEN.

- [x] T018 GREEN: delete the now-unused `_stub_schema()` — extend
  `test_mcp_registry_schemas.py`: `hasattr(registry, "_stub_schema")` is
  `False` and `grep -c "_stub_schema" digital_twins/mcp/registry.py` is 0
  (no remaining callers after T009). RED if the helper still exists, then
  GREEN: delete `_stub_schema()` from `registry.py` (line ~219) + drop its
  mention from the module docstring. Verify:
  `pytest tests/unit/test_mcp_registry_schemas.py` GREEN.

---

## Phase 4: E2E + Standing Guards

**Purpose**: end-to-end proof across auth → dispatch → body → audit, and
the two standing guards.

- [x] T019 RED: integration e2e — create
  `tests/integration/test_mcp_kb_tools.py` (SC-001..SC-007, the brief's
  "Test surface" e2e line): against a migrated v3 state DB in
  `tmp_path` + a real account (created via `digital_twins.auth`) + tokens
  minted through the 004 auth surface:
  (a) **service-token auth** (`DT_SERVICE_TOKEN` path or the service-token
  authenticator) → build the stdio/http `MCPContext` → `dispatch`
  `kb_search` against a **fake Qdrant client** (seeded at the dispatch
  seam): owner-scoped results in the exact field set; a second owner's
  point never appears (assert the filter);
  (b) **personal-token auth** (`DT_PERSONAL_TOKEN` for a scheduler/admin
  account) → dispatch `kb_ingest` against a **fake run_pipeline** (or a
  real one against a stub source) → an `audit_runs` row exists with
  `trigger="mcp"`, `scheduled_by=<caller-email>`, and `agent_kind`
  recorded (BR-11.5.3 / the acceptance line at requirement.md ~362–365);
  (c) `kb_health` dispatch returns the per-endpoint check list;
  (d) **both transports populate `MCPContext.config`**: a dispatch driven
  through the stdio `serve` path and through the http handler (on
  `127.0.0.1:0`, the 003/006 harness pattern) each sees a non-`None`
  config, while a directly-constructed `MCPContext(config=None)` fails
  closed with `config_not_loaded` (SC-007). RED (the bodies still return
  `not_implemented_yet`). Verify: `pytest
  tests/integration/test_mcp_kb_tools.py` RED. GREEN: the Phase-1/2/3 code
  makes it pass (this task commits the test; implementation is done).
  Verify: GREEN.

- [x] T020 verify: portability guard — 007 adds **zero** shipped files
  (all changes are in existing `digital_twins/` modules; no new
  non-Python artifacts), so `tests/integration/test_portability.py`'s
  `SHIPPED`/`SHIPPED_NON_PY` lists are untouched — verify it stays green
  (NFR-13; plan "Constitution Compliance"). No red step; run
  `pytest tests/integration/test_portability.py` and record the result in
  the SDD ledger (`.superpowers/sdd/007-kb-search-chat/progress.md`).

- [x] T021 verify: knob-docs guard — zero new knobs (007 reuses
  `qdrant.*`/`llm.*`/`embedding.*`/`sources.*`), so
  `tests/unit/test_knob_docs.py` must pass **unchanged** (constitution IV
  vacuous — spec "Constitution Compliance"). No red step; run
  `pytest tests/unit/test_knob_docs.py` and record green.

- [x] T022 verify: full suite green — run the **entire** `pytest` suite on
  the worktree: all of `test_mcp_config_plumbing.py`,
  `test_mcp_registry_schemas.py`, `test_mcp_kb_search.py`,
  `test_mcp_kb_chat.py`, `test_mcp_kb_ingest.py`,
  `test_mcp_kb_health.py`, `test_mcp_kb_tools.py` green; both standing
  guards green; all 001–006 tests still green (baseline **762** at main @
  7fd350b; 007 adds ~40–60 new tests → expect ~800+ total, SC-008). The
  version stays `0.6.0` until T023 (R8/R15). Record results + counts in
  the SDD ledger.

---

## Phase 5: Release

**Purpose**: version + changelog (in-branch; the tag happens only after
the merge gate — always stop and ask the owner before merging to main).

- [ ] T023 GREEN: version bump + CHANGELOG entry — bump
  `digital_twins/__init__.py` `__version__` `0.6.0` → `0.7.0` (the single
  source of truth; `digital-twins --version` follows) and add a `0.7.0`
  section to `CHANGELOG.md` in Keep-a-Changelog style, host-neutral
  (NFR-13): Added — the four real MCP KB tools (`kb_search` owner-scoped
  Qdrant search, `kb_chat` surface (501 until LLM configured),
  `kb_ingest` with `trigger="mcp"` audit + `agent_kind`, `kb_health`
  wrapping `run_health_checks`), full registry `inputSchema`s, and
  `MCPContext.config` populated by both transports; Changed — replaced the
  four BR-10 stub bodies from 004. Verify:
  `pytest tests/unit/test_mcp_config_plumbing.py tests/integration/test_mcp_kb_tools.py`
  still green + `python -m digital_twins --version` prints 0.7.0.

- [ ] T024 final whole-branch review — a separate subagent reviews the
  whole `007-kb-search-chat` branch against spec.md/plan.md/007-brief.md
  (diff, test evidence, ledger). This task records the step only; the
  review happens as its own dispatch (R9: direct subagent, no workflow).
  After the review passes, stop and ask the owner for the merge gate
  (never merge without the owner's go-ahead).

---

## Dependencies & Execution Order

- **Phase 1**: T001 → T002/T003 (parallel, disjoint files) → T004.
- **Phase 2**: T005–T008 (parallel schema builders) → T009 → T018.
- **Phase 3**: each RED task (T010/T012/T014/T016) is self-contained and
  can be written in parallel; each GREEN (T011/T013/T015/T017) depends on
  its own RED + T001 (`ctx.config`). T011's shared helpers
  (`_resolve_qdrant_client`, pooled `_embed_query`) are reused by T015.
- **Phase 4**: T019 depends on all of Phases 1–3; T020/T021 are
  cross-cutting verify-only; T022 last.
- **Phase 5**: T023 depends on T022; T024 last (merge gate after).

Within every task: RED committed and failing **before** GREEN
(constitution III). Commit after each task or logical group; keep
`specs/007-kb-search-chat/tasks.md` checkboxes in sync with commits
(`sed -i` + `git commit`, the 006-ledger convention).

## Notes

- No task spans more than ~3 files; each is a single committable unit.
- `digital_twins/web/`, `digital_twins/ingest/`, `digital_twins/health.py`,
  `digital_twins/accounts.py`, and `digital_twins/config/` are **NOT
  modified** — 007 only adds to `digital_twins/mcp/` + `cli.py` +
  `__init__.py` (version).
- `run_pipeline`'s exact signature (verified,
  `ingest/pipeline.py:105`): `run_pipeline(cfg, db, qdrant, embedder=None,
  neo4j=None, source_names=None, max_items=None, dry_run=False,
  trigger="manual", scheduled_by="system", owner=None)` — 007 calls it
  verbatim with `trigger="mcp"`, `neo4j` at its default (Qdrant-only).
- Version stays `0.6.0` through Phases 1–4; the bump is T023 (merge-time,
  R8/R15). The tag `v0.7.0` happens only after the merge gate.
- Standing guards (`test_portability.py`, `test_knob_docs.py`) must stay
  green through every phase — re-run after any code change that could
  touch them (T020/T021 are the explicit checkpoints).
