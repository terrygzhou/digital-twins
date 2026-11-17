"""Unit tests: ``catalog_version`` stamped on MENTIONED and REL edges
via ``entities.materialize()``.

Pins that every edge ``materialize()`` writes carries the catalog
version alongside the prompt version:

* MENTIONED: ``ON CREATE SET ... m.catalog_version = $cv ...`` —
  ``cv`` = ``extraction.get("catalog_version") or ""``.
* REL: MERGE property map includes ``catalog_version: $cv``.

The ``catalog_version`` key arrives on the extraction dict produced by
``extract()`` (a concurrent change adds it to the return value). When
the key is absent (older extraction data) the floor is ``""`` (empty
string, not None) — ``materialize()`` normalises a missing key to an
empty string.

Hermetic: fake driver + ``tmp_path`` YAML only, no real LLM or Neo4j.
"""
from __future__ import annotations

import textwrap

from digital_twins.ingest.entities import materialize


# ── fakes --------------------------------------------------------------------


class _FakeResult:
    """Mimics a neo4j Result object: ``.consume()`` raises
    ``AttributeError`` (no ``SummaryCounters``), so ``_run()``'s
    ``try/except AttributeError`` treats it as 0 writes."""

    def consume(self):
        raise AttributeError("no SummaryCounters in fake result")


class _FakeSession:
    """Records ``s.run(cypher, **params)`` calls on the driver's
    ``run_calls`` list; returns a ``_FakeResult``."""

    def __init__(self, driver):
        self._driver = driver

    def run(self, cypher, **params):
        self._driver.run_calls.append((cypher, params))
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDriver:
    """Driver-shaped fake: ``driver.session()`` returns a context-manager
    session that records every ``(cypher, params)`` pair on
    ``run_calls``."""

    def __init__(self):
        self.run_calls: list[tuple[str, dict]] = []

    def session(self, **kwargs):
        return _FakeSession(self)


# ── helpers --------------------------------------------------------------------


def _write_ontology(tmp_path, name="kb.ontology.yml"):
    """Write a user ontology YAML with a 021 catalog_version."""
    p = tmp_path / name
    p.write_text(
        textwrap.dedent(
            """
            catalog_version: "2026-08-24.1"
            entity_types:
              - id: building_block
                desc: "A constituent component of a product."
            relations:
              - predicate: composed_of
                from: [product, building_block]
                to: [building_block]
                desc: "built from smaller blocks."
            """
        ),
        encoding="utf-8",
    )
    return str(p)


def _extraction_with_catalog() -> dict:
    """Fake extraction dict mimicking what ``extract()`` returns:
    two entities + one REL + both version stamps."""
    return {
        "entities": [
            {"name": "Widget", "type": "product", "desc": "a widget"},
            {"name": "Block A", "type": "building_block", "desc": "a block"},
        ],
        "relations": [
            {"subject": "Widget", "predicate": "composed_of", "object": "Block A"},
        ],
        "prompt_version": "b.021-2026-08-24.1",
        "catalog_version": "b.021-2026-08-24.1",
    }


def _mentioned_call(driver):
    """Find the MENTIONED CREATE Cypher call: the one containing
    ``MERGE (si)-[m:MENTIONED]->(e)``."""
    for cypher, params in driver.run_calls:
        if "MERGE (si)-[m:MENTIONED]->(e)" in cypher:
            return cypher, params
    raise AssertionError(
        "No MENTIONED CREATE Cypher call found in fake driver run_calls: "
        f"{[c for c, _ in driver.run_calls]}"
    )


def _rel_call(driver):
    """Find the REL MERGE Cypher call: the one containing
    ``MERGE (e1)-[r:REL``."""
    for cypher, params in driver.run_calls:
        if "MERGE (e1)-[r:REL" in cypher:
            return cypher, params
    raise AssertionError(
        "No REL MERGE Cypher call found in fake driver run_calls: "
        f"{[c for c, _ in driver.run_calls]}"
    )


# ── 1: catalog_version stamped on MENTIONED + REL -----------------------------


def test_catalog_version_stamped_on_mentioned_and_rel_edges(tmp_path):
    """021: a 021 catalog (``catalog_version: 2026-08-24.1``) stamps
    ``catalog_version`` on both MENTIONED (ON CREATE SET) and REL
    (MERGE property map) edges via ``materialize()``."""
    path = _write_ontology(tmp_path)
    driver = FakeDriver()

    materialize(
        channel="test",
        item_id="item-001",
        content_hash="abc123",
        extraction=_extraction_with_catalog(),
        run_id="run-1",
        captured_at="2026-01-01T00:00:00",
        driver=driver,
        cfg={"domain_schema": True, "extraction.ontology": path},
    )

    # MENTIONED CREATE
    mentioned_cypher, mentioned_params = _mentioned_call(driver)
    assert "m.catalog_version = $cv" in mentioned_cypher, (
        f"MENTIONED CREATE must SET m.catalog_version = $cv; got:\n{mentioned_cypher}"
    )
    assert mentioned_params["cv"] == "b.021-2026-08-24.1", (
        f"MENTIONED params cv must be 'b.021-2026-08-24.1'; got {mentioned_params.get('cv')!r}"
    )

    # REL MERGE
    rel_cypher, rel_params = _rel_call(driver)
    assert "catalog_version: $cv" in rel_cypher, (
        f"REL MERGE property map must include catalog_version: $cv; got:\n{rel_cypher}"
    )
    assert rel_params["cv"] == "b.021-2026-08-24.1", (
        f"REL params cv must be 'b.021-2026-08-24.1'; got {rel_params.get('cv')!r}"
    )


# ── 2: catalog_version floor when no ontology ---------------------------------


def test_catalog_version_floor_when_no_ontology(tmp_path):
    """No ``extraction.ontology`` in cfg → the 020 floor applies. The
    extraction dict carries ``catalog_version: "b.020-floor"``;
    ``materialize()`` stamps it on both MENTIONED and REL edges."""
    driver = FakeDriver()
    extraction = {
        "entities": [
            {"name": "Widget", "type": "product", "desc": "a widget"},
        ],
        "relations": [
            {"subject": "Widget", "predicate": "composed_of", "object": "Block A"},
        ],
        "prompt_version": "b.020-floor",
        "catalog_version": "b.020-floor",
    }

    materialize(
        channel="test",
        item_id="item-002",
        content_hash="def456",
        extraction=extraction,
        run_id="run-2",
        captured_at="2026-01-01T00:00:00",
        driver=driver,
        cfg={"domain_schema": True},  # no extraction.ontology → 020 floor
    )

    mentioned_cypher, mentioned_params = _mentioned_call(driver)
    assert "m.catalog_version = $cv" in mentioned_cypher, (
        f"MENTIONED CREATE must SET m.catalog_version = $cv; got:\n{mentioned_cypher}"
    )
    assert mentioned_params["cv"] == "b.020-floor", (
        f"MENTIONED params cv must be 'b.020-floor'; got {mentioned_params.get('cv')!r}"
    )

    rel_cypher, rel_params = _rel_call(driver)
    assert "catalog_version: $cv" in rel_cypher, (
        f"REL MERGE property map must include catalog_version: $cv; got:\n{rel_cypher}"
    )
    assert rel_params["cv"] == "b.020-floor", (
        f"REL params cv must be 'b.020-floor'; got {rel_params.get('cv')!r}"
    )


# ── 3: backward compat — missing catalog_version key ---------------------------


def test_catalog_version_backward_compat_missing_key(tmp_path):
    """Older extraction data (no ``catalog_version`` key) → the floor is
    ``""`` (empty string, not None). ``materialize()`` must not crash and
    must stamp ``cv=""`` on the MENTIONED edge."""
    driver = FakeDriver()
    extraction = {
        "entities": [
            {"name": "Widget", "type": "product", "desc": "a widget"},
        ],
        "relations": [],
        "prompt_version": "b.020-floor",
        # NOTE: no catalog_version key — simulating older extraction data
    }
    assert "catalog_version" not in extraction, \
        "test precondition: extraction must lack catalog_version"

    materialize(
        channel="test",
        item_id="item-003",
        content_hash="ghi789",
        extraction=extraction,
        run_id="run-3",
        captured_at="2026-01-01T00:00:00",
        driver=driver,
        cfg={"domain_schema": True},
    )

    mentioned_cypher, mentioned_params = _mentioned_call(driver)
    assert "m.catalog_version = $cv" in mentioned_cypher, (
        f"MENTIONED CREATE must SET m.catalog_version = $cv; got:\n{mentioned_cypher}"
    )
    assert mentioned_params["cv"] == "", (
        f"MENTIONED params cv must be '' (empty string, not None); got {mentioned_params.get('cv')!r}"
    )
    assert mentioned_params["cv"] is not None, \
        "cv must be an empty string, not None"
