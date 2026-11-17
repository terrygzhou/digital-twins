# Plan: 021 — Graph insights (analytics layer over 018's REL edges)

## Summary

Adds a read-only analytical layer on top of 018's REL edges: community
detection, surprising-link scoring, and knowledge-gap detection,
materialized as extra Neo4j labels (`:Community`, `:Surprising`,
`:Gap`) and exposed via the new MCP tool `kb_graph_insights` and the new
Web endpoint `GET /api/v1/graph-insights`. All features ship
**default-off**: the master knob `insights.enabled: false` (default)
means no scheduled work runs and no labels are written, so every
existing deployment is byte-for-byte unchanged when the feature is off.
The MCP `refresh: true` path and the Web endpoint are on-demand and work
independently of the knob; both return a clean `{"ok": true, "empty":
true}` shape when there is nothing to report.

This spec is a **continuation**: the 021 knob group
(`insights.enabled` / `insights.frequency` / `insights.surprise_threshold`
/ `insights.gap_degree`) is already registered in `config/knobs.py`
(`GROUP_INSIGHTS`) and `config/schema.py`, and the core module
`digital_twins/ingest/graph_insights.py` (`compute_insights` /
`read_insights` / `edges_present`) already exists in the tree. The
remaining work is wiring: the MCP tool surface, the Web endpoint, the
002 scheduler hook, the optional `[insights]` extra, and the
RED-first test suite.

## Approach

1. **Knobs (verify, not add)** — the `insights.*` group already exists
   in `config/knobs.py` and `config/schema.py` (group constant
   `GROUP_INSIGHTS`). No new knob registration is required; the RED
   test asserts the four knobs are present with the spec's defaults and
   that `test_knob_docs.py` (T027) stays green.
2. **Core module (verify + extend)** — `digital_twins/ingest/`
   `graph_insights.py` already implements `compute_insights(driver,
   cfg, *, run_id=None)` and `read_insights(driver, cfg)`, with a
   networkx component fallback and `{"ok": true, "empty": true, ...}`
   payloads for the empty/absent-driver cases (FR-6). Confirm the
   APOC `apoc.community.louvain` path is attempted before the networkx
   fallback, and that all writes are idempotent MERGE/SET (NFR-1).
3. **MCP tool** (`digital_twins/mcp/registry.py` +
   `digital_twins/mcp/dispatch.py`): add `kb_graph_insights` with
   inputSchema `{refresh?: boolean (default false)}`. The body calls
   `compute_insights` when `refresh: true`, `read_insights` otherwise.
   Role gate: `query_status` (reader may read; admin may refresh).
   Errors: `permission_denied`, `config_not_loaded`.
4. **Web endpoint** (`digital_twins/web/app.py`): add
   `GET /api/v1/graph-insights` — the token-gated read path, always
   `read_insights` (no recompute on the default read path). Returns
   `{"ok": true, "communities": [], "surprising": [], "gaps": []}`
   when off or empty.
5. **Scheduler hook** (`digital_twins/scheduler/loop.py`): when
   `insights.enabled: true` and `insights.frequency` is `daily`/`weekly`
   (not `manual`), the 002 preset schedule fires a
   `compute_insights` pass (`trigger="schedule"`). `manual` frequency
   (and the off knob) register no schedule at all.
6. **Optional extra** (`pyproject.toml`): add `insights = ["networkx"]`
   — additive, MINOR; base install pulls nothing new.
7. **Tests (Test-First, constitution Principle III)**: RED-first knob
   test first (workstream 1), then core, MCP, Web, and scheduler
   suites. Guards: `test_portability.py` (T006) and `test_knob_docs.py`
   (T027) stay green; NFR-13 — no host paths anywhere in the new
   code.

## Files touched

- `digital_twins/ingest/graph_insights.py` (verify + extend; APOC
  preference, `insights.min_entities` no-op guard)
- `digital_twins/mcp/registry.py` (`kb_graph_insights` entry + schema)
- `digital_twins/mcp/dispatch.py` (tool body + `TOOL_CAPABILITIES`)
- `digital_twins/web/app.py` (`GET /api/v1/graph-insights` route)
- `digital_twins/scheduler/loop.py` (021 schedule hook)
- `pyproject.toml` (`insights` optional extra)
- `tests/unit/test_insights.py` (new), `tests/integration/test_mcp_insights.py`
- `CHANGELOG.md` (0.16.0 entry), `digital_twins/__init__.py` (version
  bump), `specs/021-.../tasks.md`, `.superpowers/sdd/021-.../progress.md`

## Open questions

1. `insights.frequency` is a 3-valued enum
   (`daily` | `weekly` | `manual`): 002's scheduler cron is the
   *override*, the knob is the *default* — the spec's default is kept;
   no change.
2. v1 payload is **top-N-only** (no per-node drilldown); drilldown is
   a 021.1 follow-up. The spec's default is kept.
3. The `insights.*` knobs are already registered in
   `config/knobs.py` / `config/schema.py` under `GROUP_INSIGHTS` — no
   new registration is needed; the RED test verifies the four knobs are
   present with the spec's defaults (no fix in this session beyond
   verification).

## Semver

MINOR (0.15.0 → 0.16.0): a new optional feature gated on a default-off
knob (`insights.enabled: false`); the new optional extra
`digital-twins[insights]` is additive. No breaking change to any
existing tool, endpoint, or knob.
