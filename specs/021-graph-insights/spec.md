# Feature 021: Graph insights (analytics layer over 018's REL edges)

> **License note** (NFR-13 / constitution I): this spec is an
> inspiration-level design reference only. No code, prompts, or test
> fixtures are ported from `nashsu/llm_wiki` (GPL-3.0) into this MIT
> package.

## User Scenarios & Testing

018 materialized `REL` edges in Neo4j (entity ↔ entity). This feature
adds an *analytical* layer over those edges — communities, surprising
links, knowledge gaps — computed on a schedule, surfaced via MCP
(`kb_graph_insights`) and the Web (`GET /api/v1/graph-insights`),
stored as new Neo4j labels (`:Community`, `:Surprising`, `:Gap`).
No change to 018's REL edges themselves (018 is schema-neutral;
021 reads, never writes, REL edges).

**Primary scenario**

1. `insights.enabled: true` (default `false` — off, no scheduled
   work, byte-for-byte unchanged).
2. On a schedule (`insights.frequency`: `daily` | `weekly` |
   `manual`, default `daily`), the scheduler invokes
   `digital_twins/ingest/graph_insights.py::compute_insights()`.
3. `compute_insights()` runs a single graph pass:
   - **Community detection** — Louvain (via APOC
     `apoc.community.louvain` when the APOC jar is available,
     otherwise a pure-Python `networkx` fallback reading the graph
     into memory). Communities are materialized as
     `(:Community {id, size, centroid_entity, label})` with
     `[:IN_COMMUNITY]` edges to member entities.
   - **Surprising links** — for each REL edge, compute a
     "surprise score": 1 − (source overlap × Adamic-Adar
     co-occurrence × type affinity). High scores (above a
     configurable threshold) are materialized as
     `(:Surprising {edge_key, score, reason})` with
     `[:SURPRISED]` edges to the two endpoint entities.
   - **Knowledge gaps** — entities with `degree < threshold`
     (default 2) but ≥1 incoming REL are flagged as `(:Gap
     {entity_name, degree, note})` (under-connected entities —
     candidates for deeper source ingestion).
4. `kb_graph_insights` MCP tool and `GET /api/v1/graph-insights`
   return a JSON payload: `{communities: [...], surprising:
   [...], gaps: [...]}` with counts + top-N per category.

**Edge cases**

- `insights.enabled: false` (default) → no scheduled work, no
  new labels in Neo4j, MCP tool returns `{"ok": true,
  "empty": true}` (no error), the Web endpoint returns `{"ok":
  true, "communities": [], "surprising": [], "gaps": []}`.
- APOC unavailable (no APOC jar in the operator's Neo4j) → the
  `networkx` fallback is used automatically; the MCP tool's
  response includes `"algorithm": "networkx"` (vs `"apoc"`).
- Graph with <10 entities (below `insights.min_entities`,
  default 10) → `compute_insights()` is a no-op (no labels
  written), the MCP tool returns `{"ok": true, "empty": true,
  "reason": "graph_too_small"}`.
- NFR-13: no host paths in any label property or in the MCP
  response.

## Functional Requirements

- **FR-1**: Add `digital_twins/ingest/graph_insights.py`:
  `compute_insights(cfg, driver) -> dict`. The function:
  - Loads the entity graph into memory (or via APOC when
    available).
  - Runs community detection (Louvain; `apoc.community.louvain`
    when APOC is present, else `networkx.community
    .louvain_communities`).
  - Computes the surprise score per REL edge
    (`1 - source_overlap * adamic_adaar * type_affinity`);
    `adamic_adaar` is the Adamic-Adar co-occurrence between
    the two endpoint entities, `type_affinity` is a static
    per-pair table (e.g. `(Product, Organization) → 0.8`,
    default 0.5 for unknown pairs).
  - Flags gaps (under-connected entities, default degree < 2).
  - MERGEs the resulting `:Community`, `:Surprising`, `:Gap`
    labels into Neo4j (all idempotent MERGEs; re-running the
    same pass writes 0 new labels — NFR-1).
  - Returns `{"communities": N, "surprising": M, "gaps": K,
    "algorithm": "apoc" | "networkx"}`.
- **FR-2**: Add a new MCP tool `kb_graph_insights` to
  `digital_twins/mcp/registry.py` + `digital_twins/mcp/
  dispatch.py`: inputSchema `{refresh: bool (optional, default
  false)}`. When `refresh: true`, invoke `compute_insights()`
  (synchronous); when `refresh: false`, return the most recent
  computed payload (no recompute — reads the existing labels).
- **FR-3**: Add a new Web endpoint `GET /api/v1/graph-insights`
  (015's HTTP layer) returning `{"ok": true, "communities":
  [...], "surprising": [...], "gaps": [...], "algorithm":
  "apoc" | "networkx"}`. Role: any authenticated user (same
  gate as `kb_search` — `reader`).
- **FR-4**: Add knob `insights.frequency` (enum `daily` |
  `weekly` | `manual`, default `daily` — but only effective
  when `insights.enabled: true`) and `insights.enabled` (bool,
  default `false`). The scheduler (002 semantics — 021 does
  not change it) picks up the new schedule name; the 002
  scheduler treats it as a normal scheduled job.
- **FR-5**: Add knob `insights.surprise_threshold` (float,
  default 0.7) and `insights.gap_degree` (int, default 2)
  (gaps flagged when an entity's REL degree < `insights.gap_
  degree`).
- **FR-6**: Test-First. Unit tests use a small in-memory Neo4j
  graph (the existing test fixture) and assert:
  - A graph with 2 clearly-separated communities → `compute_
    insights()` returns 2 `:Community` labels, each with the
    correct member entities.
  - A graph with a high-score surprise edge → `:Surprising`
    label written with the correct score; a low-score edge →
    no `:Surprising` label (below threshold).
  - An entity with degree 1 (below `insights.gap_degree`) →
    `:Gap` label written.
  - Re-running `compute_insights()` with the same graph → 0
    new labels (idempotent MERGEs; NFR-1).
  - `kb_graph_insights` with `refresh: false` on an empty graph
    → `{"ok": true, "empty": true}` (no error).
  - `tests/unit/test_knob_docs.py` green.

## Non-goals

- No change to 018's REL edges — 021 is a *read-only*
  analytical layer; REL edges are the input, never the output.
- No OCR / no LLM calls (the surprise-score formula is
  deterministic, not model-based).
- No new vector store; the labels ride on the existing Neo4j
  graph (Neo4j is schema-less, no migration).
- No change to 002's scheduler semantics (021 adds a new
  schedule name; the scheduler itself is unchanged).
- No real-time graph queries — the labels are a materialized,
  scheduled snapshot (the MCP `refresh: true` triggers a
  recompute on demand).
- No change to 007's `kb_search` response shape (021 does not
  alter `kb_search`; it adds a separate `kb_graph_insights`
  tool).

## Success Criteria

- With `insights.enabled: true` + a 500-entity graph, a single
  `compute_insights()` pass produces ≥1 `:Community`, ≥1
  `:Surprising` (when the threshold is crossed), and ≥1 `:Gap`
  (when any under-connected entity exists).
- `kb_graph_insights` with `refresh: true` triggers a
  recompute within the call (synchronous); `refresh: false`
  returns the cached payload without a recompute.
- `GET /api/v1/graph-insights` returns the same payload as the
  MCP tool (the two surfaces read the same labels).
- `insights.enabled: false` (default) → no scheduled work, no
  new labels, MCP tool returns `{"ok": true, "empty": true}`.
- Re-running `compute_insights()` on an unchanged graph → 0
  new labels (NFR-1).
- `tests/unit/test_knob_docs.py` green.

## Key Entities

- `:Community {id, size, centroid_entity, label}` — new Neo4j
  label (MERGEd by `compute_insights()`).
- `:Surprising {edge_key, score, reason}` + `[:SURPRISED]` —
  new Neo4j label + edge.
- `:Gap {entity_name, degree, note}` — new Neo4j label.
- `compute_insights()` — new function in
  `digital_twins/ingest/graph_insights.py`.
- `kb_graph_insights` — new MCP tool.
- `GET /api/v1/graph-insights` — new Web endpoint.
- `insights.enabled`, `insights.frequency`,
  `insights.surprise_threshold`, `insights.gap_degree` — new
  config knobs.

## Assumptions

- The Neo4j graph (when present) is small enough to read into
  memory for the `networkx` fallback (the APOC path, when
  available, avoids the in-memory read — the APOC algorithm
  runs server-side).
- APOC may or may not be installed in the operator's Neo4j —
  the `networkx` fallback is automatic, no operator action.
- `networkx` is pulled in by the optional extra
  `digital-twins[insights]` (the base distribution does not
  install it — NFR-13 portability).
- The surprise-score formula (`1 - source_overlap *
  adamic_adaar * type_affinity`) is a reasonable v1 heuristic;
  a tunable per-pair `type_affinity` table is out of scope for
  v1 (a follow-up feature).

## Open questions (max 3)

- [NEEDS CLARIFICATION: should `insights.frequency` be a free
  cron expression (operator-defined) instead of the fixed
  `daily | weekly | manual` enum?] — default: keep the 3-value
  enum (the 002 scheduler's cron semantics are already
  operator-configurable per schedule name; the enum is the
  default preset, the cron is the override).
- [NEEDS CLARIFICATION: should `kb_graph_insights` accept a
  `community_id` / `gap_name` filter for drill-down, or is the
  top-N-only payload sufficient for v1?] — default: top-N-only
  (drill-down is a v1.1 add).

## Semver

MINOR. New optional feature behind a default-off knob
(`insights.enabled`); when off, no scheduled work, no new
labels, no new MCP tool body (the tool returns `{"ok": true,
"empty": true}` — a safe, no-op surface). New optional extra
(`digital-twins[insights]`) is additive.


