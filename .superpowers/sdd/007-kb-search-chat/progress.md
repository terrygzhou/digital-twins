# Feature 007 — SDD progress (per-task detailed record)

Branch `007-kb-search-chat` (worktree `.worktrees/007-kb-search-chat`).
Phases 1–3 complete in earlier spans; Phase 4 (T018–T022) recorded below.

## Phase 4

- **T018** (no-op confirm): `grep -c "_stub_schema"
  digital_twins/mcp/registry.py` → `0` (no remaining callers; the helper was
  deleted in an earlier span).
- **T018b** (2 commits):
  - `3aca3b8` — delete `tests/unit/test_mcp_stubs.py` (the four BR-10 stubs no
    longer exist; their stub-shape tests are superseded by the real bodies).
  - `b3d9528` — `test_sc001_fresh_client_gets_full_tool_list` in
    `tests/integration/test_mcp_integration.py`: the 4 KB tools no longer
    assert `not_implemented_yet`; new assertion — each KB tool's `error.code`
    (if any) is NOT `not_implemented_yet` and the observed code set ⊆
    `{bad_request, not_implemented, permission_denied, run_failed,
    qdrant_unavailable, embedding_unavailable, config_not_loaded,
    source_disabled, prerequisite_missing, unknown_source}`. All 6 tests in
    the file green.
- **T019** — `039d7b7`: `tests/integration/test_mcp_kb_tools.py` (6 tests):
  (a) service-token → stdio → `kb_search` vs fake Qdrant (owner-scoped
  filter asserted; second owner's point never appears); (b) personal-token →
  stdio → `kb_ingest` vs fake `run_pipeline` → `audit_runs` row with
  `trigger="mcp"`, `scheduled_by=<caller>`, `agent_kind` in
  `per_source_counts`; (c) `kb_health` → per-endpoint check list
  (`["qdrant","neo4j","llm"]`, exact field set, all `ok=False` unconfigured);
  (d) both transports populate `MCPContext.config` (stdio `serve` + http
  handler on `127.0.0.1:0`), and `MCPContext(config=None)` fails closed with
  `config_not_loaded`. `pytest tests/integration/test_mcp_kb_tools.py` → 6
  passed.
- **T020** — `pytest tests/integration/test_portability.py` → **3 passed**
  (portability guard green; 007 adds zero shipped files, SHIPPED lists
  untouched).
- **T021** — `pytest tests/unit/test_knob_docs.py` → **20 passed** (knob-docs
  guard green; zero knob changes).
- **T022** — full suite `pytest` on the worktree → **831 passed** in 60.79s
  (0 failed, 0 skipped; SC-008 met: baseline 762 at main @ 7fd350b + 007's
  ~70 new tests). Version stays `0.6.0` until T023 (R8/R15).

## Phase 5

- **T023** — `a4d3c02`: `__version__` 0.6.0 → 0.7.0 (single source) +
  `CHANGELOG.md` 0.7.0 section (Added/Changed, host-neutral). `python -m
  digital_twins --version` → 0.7.0. Merged to main as `26b3f25`
  (merge commit; owner-ratified AGENTS.md v0.7.0 bookkeeping rides the tip).
- **T024** — final whole-branch review dispatched as a separate reviewer
  subagent on `7fd350b..26b3f25` (R9: direct subagent, no workflow).
  Verdict: **Ready to merge — With fixes** (non-blocking follow-ups).
  Reviewer independently re-ran: full suite **831 passed** in 60.56s;
  mandated 10-file subset 101/102 (1 pre-existing flake — finding I1);
  host-neutrality sweep of the diff → zero host pins; SC-001..SC-008 each
  verified against named tests; 33 commits = clean RED/GREEN pairs; no
  out-of-scope files touched. Findings (all non-blocking, scheduled in
  `.superpowers/sdd/deferred-minors/007-t024-findings.md`):
  - **I1 (Important)**: `tests/integration/test_mcp_integration.py::test_sc005_transport_parity`
    (004 test, unmodified by 007) is order-flaky under the 10-file subset
    run order — process-global credential/env coupling in `mcp_stdio.serve`
    credential resolution leaves state from an earlier test; green in
    isolation (6/6) and full suite (831/831). Fix: snapshot/restore
    credential-related process globals in an autouse integration fixture,
    or inject an explicit credential seam in the 004 test.
  - **I2 (Minor)**: `digital_twins/mcp/dispatch.py:623–632`
    `_resolve_qdrant_client` caches in module globals with no key — a
    config change in-process (new `qdrant.url`/`api_key`) reuses the stale
    client; check-then-set also races under threaded HTTP. Fix: key the
    cache on `(url, api_key)` + lock + one test.
  - **I3 (Minor)**: `digital_twins/mcp/dispatch.py:984–987`
    `_kb_ingest_body` overlays the **flat dotted** `config.schema.DEFAULTS`
    onto the nested `ctx.config`, producing a hybrid nested+flat dict,
    diverging from 006's `web/app.py::_merge_defaults` (nested expansion
    first). Functionally equivalent today (all pipeline reads go through
    `config.schema.get`); latent for any nested-section consumer. Fix:
    expand flat DEFAULTS into nested form before the overlay (in-scope
    duplicate of the ~10-line expansion; factoring into a shared helper
    would touch `web/`, out of scope for 007).
  - **I4 (Minor, bookkeeping)**: `specs/007-kb-search-chat/tasks.md`
    checkboxes T001–T004/T010–T017/T023 not synced with commits — fixed in
    the same commit that records this entry.
  No Critical findings. Branch already merged (`26b3f25`); findings I1–I3
  are follow-up slices, not merge blockers.
