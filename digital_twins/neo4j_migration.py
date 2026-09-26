"""S4 graph migration (openspec s4-graph-alignment, task 4.1).

One-shot cleanup of the pre-S4 legacy graph labels. The S4 write path
(``ingest.pipeline._upsert_graph``) writes ``:SourceItem`` nodes only; the
pre-S4 pipeline wrote ``:KbItem``/``:KbChunk`` with ``HAS_CHUNK`` edges,
and those nodes carry no data the S4 shape reads — the design decision
(spec Non-goals) is delete + re-ingest, not in-place translation.

``migrate_s4`` is idempotent and a no-op on a database that never held
the legacy labels: it reports 0 nodes deleted and exits cleanly.
"""
from __future__ import annotations

from typing import Any

_LEGACY_LABELS = ("KbItem", "KbChunk")


def migrate_s4(driver: Any, dry_run: bool = False) -> dict:
    """Remove legacy ``:KbItem`` / ``:KbChunk`` nodes.

    *dry_run*: run count queries only — no mutation — and report the
    node counts that a real run would delete.

    The driver contract is the pipeline hand-off surface (002 US2)
    after graph-driver-fix (BUG-01): the session API — ``with
    driver.session() as s: s.run(query, **params)``, whose result
    carries ``.single()``. The caller hands over a raw
    ``neo4j.GraphDatabase.driver``
    (``scheduler.loop.build_neo4j_driver``), which exposes no
    driver-level ``.run()``.
    ``:SourceItem`` nodes are never touched — the S4 write path owns
    them; a fresh database therefore migrates to {0, 0}.
    """
    result: dict = {"dry_run": dry_run}
    counts: dict[str, int] = {}
    with driver.session() as s:
        for label in _LEGACY_LABELS:
            q = f"MATCH (n:{label}) "
            counts[label] = s.run(q + "RETURN count(n) AS n").single()["n"]
        if not dry_run:
            for label in _LEGACY_LABELS:
                s.run(f"MATCH (n:{label}) DETACH DELETE n")
    if dry_run:
        result["would_delete"] = counts
    else:
        result["deleted"] = counts
    return result
