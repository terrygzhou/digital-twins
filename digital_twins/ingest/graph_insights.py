"""Materialized graph insights (021-graph-insights).

Read-only analytical layer over the Neo4j graph that 018's REL edges
populate:

- ``:Community`` labels — a materialized snapshot of the graph's
  connected components (networkx), one label per entity node.
- ``:Surprising`` labels — REL edges whose deterministic surprise
  score crosses ``insights.surprise_threshold``.
- ``:Gap`` labels — entity nodes whose REL degree is below
  ``insights.gap_degree``.

``compute_insights()`` (re)computes and MERGEs these labels;
``read_insights()`` reads the existing labels back (no recompute —
for ``kb_graph_insights`` with ``refresh: false`` and the Web
endpoint's default path).  All writes are idempotent MERGEs (NFR-1).
"""

from __future__ import annotations

from ..config.schema import get as _cfg_get


def compute_insights(driver, cfg: dict, *, run_id: str | None = None) -> dict:
    """(Re)compute and MERGE the insight labels; return a payload.

    Parameters
    ----------
    driver:
        A Neo4j driver object (``neo4j.GraphDatabase.driver``); when
        None, the "graph unavailable" payload is returned (Qdrant-only
        mode).
    cfg:
        A config-like object exposing ``get(path)``.

    Returns
    -------
    dict
        ``{"ok": True, "communities": [...], "surprising": [...],
        "gaps": [...], "algorithm": "networkx"}`` — or when the driver
        is absent / the graph is empty, ``{"ok": True, "empty": True,
        ...}`` (FR-6: empty graph → no error).
    """
    if driver is None:
        return {"ok": True, "empty": True, "communities": [],
                "surprising": [], "gaps": [], "algorithm": "networkx"}

    from networkx import Graph, connected_components  # optional dep

    surprise_threshold = float(_cfg_get(cfg, "insights.surprise_threshold")
                               or 0.7)
    gap_degree = int(_cfg_get(cfg, "insights.gap_degree") or 2)

    # 1. Load the REL edges.  The 018 edge shape carries a deterministic
    #    ``score`` when available; a missing/None score scores 0.0
    #    (below threshold → never :Surprising).
    edges: list[dict] = []
    with driver.session() as s:
        result = s.run(
            "MATCH (a)-[r:REL]->(b) "
            "RETURN a.name AS src, b.name AS dst, r.score AS score"
        )
        for record in result:
            edges.append({
                "src": record["src"],
                "dst": record["dst"],
                "score": record["score"] or 0.0,
            })

    # 2. Build the undirected component graph.
    g: Graph = Graph()
    for e in edges:
        g.add_edge(e["src"], e["dst"])
    components = sorted(connected_components(g), key=len, reverse=True)
    communities = [list(c) for c in components]

    # 3. Node REL degrees (undirected).
    degrees = dict(g.degree())

    # 4. Surprise / gap selection.
    surprising = [
        e for e in edges
        if e["src"] and e["dst"] and e["score"] >= surprise_threshold
    ]
    gaps = sorted(
        (n for n, deg in degrees.items() if deg < gap_degree),
        key=lambda n: (degrees[n], n),
    )

    # 5. MERGE the labels (idempotent — re-running produces 0 new labels).
    _merge_labels(driver, communities, surprising, gaps, run_id=run_id)

    payload = {
        "ok": True,
        "communities": communities,
        "surprising": [
            {"src": e["src"], "dst": e["dst"], "score": e["score"]}
            for e in surprising
        ],
        "gaps": gaps,
        "algorithm": "networkx",
    }
    if not edges:
        payload["empty"] = True
    return payload


def read_insights(driver, cfg: dict) -> dict:
    """Read the existing ``:Community`` / ``:Surprising`` / ``:Gap``
    labels back from Neo4j (no recompute).

    Empty graph / driver absent → ``{"ok": True, "empty": True, ...}``.
    """
    if driver is None:
        return {"ok": True, "empty": True, "communities": [],
                "surprising": [], "gaps": [], "algorithm": "networkx"}

    communities: list[list[str]] = []
    surprising: list[dict] = []
    gaps: list[str] = []

    with driver.session() as s:
        rows = s.run(
            "MATCH (n:Entity:Community) "
            "WHERE n.community_id IS NOT NULL "
            "RETURN n.community_id AS cid, collect(n.name) AS names "
            "ORDER BY cid"
        ).data()
        for row in rows:
            communities.append(
                {"id": row["cid"], "members": sorted(row["names"] or [])})

        rows = s.run(
            "MATCH (a)-[r:REL:Surprising]->(b) "
            "RETURN a.name AS src, b.name AS dst, r.score AS score"
        ).data()
        for row in rows:
            surprising.append(
                {"src": row["src"], "dst": row["dst"],
                 "score": row["score"]})

        rows = s.run(
            "MATCH (n:Entity:Gap) RETURN collect(n.name) AS names"
        ).data()
        for row in rows:
            gaps.extend(row["names"] or [])
        gaps = sorted(set(gaps))

    payload = {
        "ok": True,
        "communities": communities,
        "surprising": surprising,
        "gaps": gaps,
        "algorithm": "networkx",
    }
    if not edges_present(driver):
        payload["empty"] = True
    return payload


def edges_present(driver) -> bool:
    """True when at least one REL edge exists in the graph."""
    with driver.session() as s:
        row = s.run(
            "MATCH ()-[r:REL]()- RETURN count(r) AS n"
        ).single()
        return bool(row is not None and row["n"] and row["n"] > 0)


def _merge_labels(driver, communities, surprising, gaps,
                  run_id: str | None = None) -> None:
    """MERGE the three label families into Neo4j (idempotent).

    - ``:Community``: one node label per entity, with ``community_id``
      property = the 1-based index of its component (so read-back can
      group members).
    - ``:Surprising``: an extra label on the REL edge itself.
    - ``:Gap``: an extra node label on under-connected entities.
    """
    node_to_cid: dict[str, int] = {}
    for cid, members in enumerate(communities, start=1):
        for name in members:
            node_to_cid[name] = cid

    with driver.session() as s:
        for name, cid in node_to_cid.items():
            s.run(
                "MATCH (n:Entity) WHERE n.name = $name "
                "SET n:Community, n.community_id = $cid",
                name=name, cid=cid,
            )
        for e in surprising:
            s.run(
                "MATCH (a:Entity {name: $src})-[r:REL]->"
                "(b:Entity {name: $dst}) "
                "SET r:Surprising, r.insight_score = $score",
                src=e["src"], dst=e["dst"], score=e["score"],
            )
        for name in gaps:
            s.run(
                "MATCH (n:Entity {name: $name}) SET n:Gap",
                name=name,
            )
