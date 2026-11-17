# Solution: 021 materialized graph insights

## `digital_twins/ingest/graph_insights.py` (core module — verify + extend)

The module already exists with `compute_insights(driver, cfg, *,
run_id=None) -> dict` and `read_insights(driver, cfg) -> dict`, plus
`edges_present(driver) -> bool` and `_merge_labels(driver,
communities, surprising, gaps, run_id=None)`. The reconciliation work
is:

- **APOC preference** — `compute_insights` attempts
  `CALL apoc.community.louvain` first (server-side, no in-memory read)
  and falls back to `networkx.community.louvain_communities` only when
  the APOC jar is absent. The return payload's `"algorithm"` field is
  `"apoc"` or `"networkx"` accordingly. A mocked driver with APOC
  present asserts `"algorithm": "apoc"`; an APOC-absent driver asserts
  `"algorithm": "networkx"`.
- **Surprise score** — per REL edge: `1 - source_overlap *
  adamic_adaar * type_affinity` where `adamic_adaar` is the Adamic-Adar
  co-occurrence between the two endpoint entities and `type_affinity`
  is a static per-pair table (e.g. `(Product, Organization) → 0.8`,
  default `0.5` for unknown pairs). Edges with score ≥
  `insights.surprise_threshold` materialize as
  `(:Surprising {edge_key, score, reason})` + `[:SURPRISED]`.
- **Communities** — Louvain components materialize as
  `(:Community {id, size, centroid_entity, label})` with
  `[:IN_COMMUNITY]` edges to member entities.
- **Gaps** — entities with REL degree < `insights.gap_degree`
  (default 2) and ≥1 incoming REL materialize as `(:Gap
  {entity_name, degree, note})`.
- **Idempotent MERGE/SET** (NFR-1) — re-running the same pass writes
  0 new labels.

## `digital_twins/config/knobs.py` + `config/schema.py` (verify, not add)

The `insights.*` group is already registered under `GROUP_INSIGHTS`:

| Knob | Type | Default | Env form |
|---|---|---|---|
| `insights.enabled` | bool | `false` | `KB_INSIGHTS__ENABLED` |
| `insights.frequency` | str (enum `daily|weekly|manual`) | `daily` | `KB_INSIGHTS__FREQUENCY` |
| `insights.surprise_threshold` | float | `0.7` | `KB_INSIGHTS__SURPRISE_THRESHOLD` |
| `insights.gap_degree` | int | `2` | `KB_INSIGHTS__GAP_DEGREE` |

The RED test verifies these four are present with the spec's defaults;
`tests/unit/test_knob_docs.py` (T027) stays green.
`insights.min_entities` is **not** registered — recorded in open
questions for a follow-up; not added in this session.

## `digital_twins/mcp/registry.py` + `dispatch.py` (MCP tool)

- `_kb_graph_insights_schema()` — inputSchema `{refresh?: boolean
  (default false)}`; the description documents the
  `{"ok": true, "empty": true}` off/empty shape and the
  `"algorithm": "apoc" | "networkx"` field.
- `_kb_graph_insights_body(ctx, args)` — `refresh: true` invokes
  `compute_insights` synchronously; `refresh: false` returns the most
  recent computed payload (reads existing labels, no recompute). Role
  gate `query_status` (reader may read; admin may refresh). Errors:
  `permission_denied` (reader with `refresh: true`),
  `config_not_loaded` (config is `None`).
- `TOOL_CAPABILITIES["kb_graph_insights"] = "query_status"`.

## `digital_twins/web/app.py` (Web endpoint)

`GET /api/v1/graph-insights` — the token-gated read path, always
`read_insights` (no recompute on the default read path). Returns
`{"ok": true, "communities": [...], "surprising": [...], "gaps":
[...], "algorithm": "apoc" | "networkx"}`. Role: any authenticated
user (same gate as `kb_search` — `reader`). An unauthenticated GET is
401; an off/empty graph returns
`{"ok": true, "communities": [], "surprising": [], "gaps": []}`.

## `digital_twins/scheduler/loop.py` (scheduler hook)

When `insights.enabled: true` and `insights.frequency` is `daily` or
`weekly` (not `manual`), the 002 preset schedule fires a
`compute_insights` pass (`trigger="schedule"`). `manual` frequency and
the off knob register no schedule at all. The 002 scheduler semantics
are otherwise unchanged (non-goal).

## `pyproject.toml` (optional extra)

`insights = ["networkx"]` — additive, MINOR; base install pulls nothing
new. NFR-13: `networkx` is a declared dependency, not a host path.

## `tests/unit/test_insights.py` + `tests/integration/test_mcp_insights.py`

- **Unit** (`tests/unit/test_insights.py`, over the existing in-memory
  Neo4j fixture):
  1. 2 clearly-separated communities → 2 `:Community` labels with the
     correct member entities.
  2. A high-score surprise edge → `:Surprising` with the correct score;
     a low-score edge → no `:Surprising` label.
  3. A degree-1 entity (below `insights.gap_degree`) → `:Gap` label.
  4. Re-running `compute_insights()` on the same graph → 0 new labels
     (NFR-1).
  5. `kb_graph_insights` `refresh: false` on an empty graph →
     `{"ok": true, "empty": true}`.
  6. APOC-present vs APOC-absent driver → `"algorithm": "apoc"` vs
     `"networkx"`.
- **Integration** (`tests/integration/test_mcp_insights.py`): the MCP
  tool over the in-memory fixture — `refresh: true` triggers a
  synchronous recompute and writes labels; `refresh: false` returns the
  cached payload without a recompute.
- **Guards**: `tests/unit/test_knob_docs.py` (T027) and
  `tests/integration/test_portability.py` (T006) stay green.

## `CHANGELOG.md` + `digital_twins/__init__.py`

Bump to `0.16.0` (MINOR). CHANGELOG entry: 021 materialized graph
insights; new MCP tool `kb_graph_insights` + Web endpoint
`GET /api/v1/graph-insights`; `insights.*` knob group (default-off);
`digital-twins[insights]` optional extra.
