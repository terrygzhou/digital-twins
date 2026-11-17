# Tasks: 021 — Graph insights (analytics layer over 018's REL edges)

Constitution gate: Test-First (RED-first knob test first), default-off
feature, NFR-13 no host paths, T027 knob-docs guard, T006 portability
guard.

## 1. Knob (RED-first, constitution Principle III)

The 021 knob group (`insights.*`) is **already registered** in
`config/knobs.py` (`GROUP_INSIGHTS`) and `config/schema.py` — a partial
continuation. The first workstream is therefore a *verify* pass, not an
addition.

- [ ] Add `tests/unit/test_insights_knob.py` asserting the four knobs are
      present with the spec's defaults: `insights.enabled` (bool,
      `false`), `insights.frequency` (enum `daily|weekly|manual`,
      default `daily`), `insights.surprise_threshold` (float, `0.7`),
      `insights.gap_degree` (int, `2`).
      **Verification:** RED — the test collects and runs; `pytest
      tests/unit/test_insights_knob.py` passes against the already-
      registered knobs.
- [ ] Confirm `tests/unit/test_knob_docs.py` (T027) is green with the
      existing `insights.*` doc strings (env forms `KB_INSIGHTS__*`).
      **Verification:** `pytest tests/unit/test_knob_docs.py` green.
- [ ] Confirm `insights.min_entities` is **not** registered; record it
      in the 021 open questions for a follow-up spec (do **not** add it
      in this session — spec is the single source of truth).
      **Verification:** `grep -rn min_entities digital_twins/config/`
      returns nothing.

## 2. Core module: APOC preference + idempotent MERGE

`digital_twins/ingest/graph_insights.py` exists with
`compute_insights(driver, cfg, *, run_id=None)` and
`read_insights(driver, cfg)`. Reconcile it with the spec.

- [ ] Confirm `compute_insights` attempts `apoc.community.louvain`
      first and falls back to `networkx.community.louvain_communities`
      only when the APOC jar is absent; the return payload's
      `"algorithm"` field is `"apoc"` or `"networkx"` accordingly.
      **Verification:** a test with a mocked APOC-present driver
      asserts `"algorithm": "apoc"`; a test with APOC absent asserts
      `"algorithm": "networkx"`.
- [ ] Confirm the surprise-score formula is
      `1 - source_overlap * adamic_adaar * type_affinity` with the
      static per-pair `type_affinity` table (default `0.5`); a REL edge
      whose score ≥ `insights.surprise_threshold` is materialized as
      `(:Surprising {edge_key, score, reason})` + `[:SURPRISED]`.
      **Verification:** a unit test over the existing in-memory Neo4j
      fixture: high-score edge → `:Surprising` with correct score;
      low-score edge → no label.
- [ ] Confirm communities materialize as
      `(:Community {id, size, centroid_entity, label})` +
      `[:IN_COMMUNITY]`; gaps as `(:Gap {entity_name, degree, note})`
      for entities with REL degree < `insights.gap_degree` and ≥1
      incoming REL.
      **Verification:** the 2-seeded-community fixture yields exactly 2
      `:Community` labels with the correct member entities; a
      degree-1 entity yields a `:Gap` label.
- [ ] Confirm all writes are idempotent MERGE/SET (NFR-1).
      **Verification:** re-running `compute_insights()` on the same
      graph writes 0 new labels.
- [ ] Confirm the empty/absent-driver cases return
      `{"ok": true, "empty": true}` without error (FR-6).
      **Verification:** `kb_graph_insights` with `refresh: false` on an
      empty graph → `{"ok": true, "empty": true}`.

## 3. MCP tool `kb_graph_insights`

- [ ] Add the `kb_graph_insights` entry to
      `digital_twins/mcp/registry.py` with inputSchema
      `{refresh?: boolean (default false)}`; the description documents
      the `{"ok": true, "empty": true}` off/empty shape and the
      `"algorithm": "apoc" | "networkx"` field.
      **Verification:** `build_tool_registry()` includes
      `kb_graph_insights` with the FR-2 schema.
- [ ] Add the `_kb_graph_insights_body(ctx, args)` to
      `digital_twins/mcp/dispatch.py`: `refresh: true` invokes
      `compute_insights` synchronously; `refresh: false` returns the
      most recent computed payload (reads the existing labels, no
      recompute). Role gate `query_status` (reader may read; admin may
      refresh). Errors: `permission_denied` (reader with
      `refresh: true`), `config_not_loaded` (config is `None`).
      **Verification:** unit tests over the existing in-memory fixture
      — `refresh: true` recomputes and writes labels; `refresh: false`
      on an empty graph returns `{"ok": true, "empty": true}`; a
      `reader` with `refresh: true` is refused.
- [ ] Add `kb_graph_insights` to `TOOL_CAPABILITIES` (capability
      `query_status`).
      **Verification:** `test_mcp_dispatch` (existing) passes with the
      new tool registered.

## 4. Web endpoint `GET /api/v1/graph-insights`

- [ ] Add the `GET /api/v1/graph-insights` route to
      `digital_twins/web/app.py` — the token-gated read path,
      always `read_insights` (no recompute on the default read path).
      Returns `{"ok": true, "communities": [...], "surprising":
      [...], "gaps": [...], "algorithm": "apoc" | "networkx"}`. Role:
      any authenticated user (same gate as `kb_search` — `reader`).
      **Verification:** a token-authenticated GET returns the same
      payload as the MCP `refresh: false` read; an unauthenticated GET
      is 401; an off/empty graph returns
      `{"ok": true, "communities": [], "surprising": [], "gaps": []}`.

## 5. Scheduler hook (002 semantics, 021 adds a schedule name)

- [ ] In `digital_twins/scheduler/loop.py`, when
      `insights.enabled: true` and `insights.frequency` is `daily` or
      `weekly` (not `manual`), the 002 preset schedule fires a
      `compute_insights` pass (`trigger="schedule"`). `manual`
      frequency and the off knob register no schedule at all.
      **Verification:** a test with `insights.enabled: true` +
      `frequency: daily` asserts the 021 schedule is registered and
      fires a pass; a test with `insights.enabled: false` or
      `frequency: manual` asserts no schedule is registered.
- [ ] Confirm the 002 scheduler is otherwise unchanged (021 adds a
      schedule name; the scheduler itself is unchanged — non-goal).
      **Verification:** the 002 preset/schedule test suite stays green.

## 6. Optional extra `digital-twins[insights]`

- [ ] Add the `insights = ["networkx"]` optional extra to
      `pyproject.toml`. Base install pulls nothing new. NFR-13:
      `networkx` is a declared dependency, not a host path.
      **Verification:** `pip install -e ".[insights]"` pulls
      `networkx`; the base `pip install .` does not.
- [ ] Confirm `test_portability.py` (T006) and NFR-13 guards are
      green with the new extra.
      **Verification:** `pytest tests/integration/test_portability.py`
      green.

## 7. Tests (Test-First, FR-6)

- [ ] `tests/unit/test_insights.py`: unit tests over the existing
      in-memory Neo4j fixture — 2 clearly-separated communities → 2
      `:Community` labels; high-score surprise edge → `:Surprising`
      with correct score, low-score edge → no label; a degree-1 entity
      → `:Gap` label; re-running `compute_insights()` → 0 new labels
      (NFR-1); `kb_graph_insights` `refresh: false` on an empty graph →
      `{"ok": true, "empty": true}`.
      **Verification:** `pytest tests/unit/test_insights.py` green.
- [ ] `tests/integration/test_mcp_insights.py`: integration test for
      the MCP tool over the in-memory fixture — `refresh: true`
      triggers a synchronous recompute and writes labels;
      `refresh: false` returns the cached payload without a recompute.
      **Verification:** `pytest tests/integration/test_mcp_insights.py`
      green.
- [ ] Confirm `tests/unit/test_knob_docs.py` (T027) and
      `tests/integration/test_portability.py` (T006) stay green.
      **Verification:** both guards green.

## 8. Semver + release note

- [ ] Bump `digital_twins/__init__.py` to `0.16.0` (MINOR — new
      optional feature gated on a default-off knob; the new optional
      extra is additive).
      **Verification:** `digital-twins --version` reports `0.16.0`.
- [ ] Add the `0.16.0` entry to `CHANGELOG.md` (021 materialized graph
      insights; new MCP tool + Web endpoint; `insights.*` knob group;
      `digital-twins[insights]` extra).
      **Verification:** the CHANGELOG entry matches the shipped change.

## 9. Bookkeeping

- [ ] Mark every `- [ ]` task in this file as done when its workstream
      is complete.
      **Verification:** `grep -c '^- \[ \]' tasks.md` reports 0
      remaining.
- [ ] Create/update `.superpowers/sdd/021-graph-insights/progress.md`
      (the SDD ledger).
      **Verification:** the progress ledger exists and matches the
      commits.

## Verification summary

- `pytest tests/unit/test_insights_knob.py tests/unit/test_insights.py
  tests/unit/test_knob_docs.py tests/unit/test_mcp_dispatch.py` green.
- `pytest tests/integration/test_mcp_insights.py
  tests/integration/test_portability.py` green.
- `digital-twins --version` reports `0.16.0`.
- `insights.enabled: false` (default) → no scheduled work, no new
  labels, MCP `{"ok": true, "empty": true}`, Web
  `{"ok": true, "communities": [], "surprising": [], "gaps": []}`.
- No host paths anywhere in the new code (NFR-13).
