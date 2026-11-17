"""Unit tests for digital_twins.ingest.catalog (021-building-block-ontology).

Hermetic: every catalog file is written to a tmp_path fixture; no host
values, no network (NFR-13).  Written RED-first per tasks.md §1.1 —
``digital_twins/ingest/catalog.py`` does not exist yet.
"""
from __future__ import annotations

import textwrap

import pytest

from digital_twins.ingest.catalog import (
    DEFAULT_020_CATALOG,
    Catalog,
    CatalogError,
    effective_prompt_version,
    load_catalog,
    render_domain_prompt,
)

# ── fixtures ---------------------------------------------------------------


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


# ── 020 floor ----------------------------------------------------------------


def test_default_020_catalog_shapes_floor():
    """The 020 floor is baked in as a module-level catalog: the 5 domain
    entity types + the 6 domain predicates."""
    types = {t.id for t in DEFAULT_020_CATALOG.entity_types}
    assert types == {
        "product", "customer", "competitor", "standard", "cert",
    }
    preds = {r.predicate for r in DEFAULT_020_CATALOG.relations}
    assert preds == {
        "competes_with", "supplied_to", "complies_with",
        "substitutes_for", "manufactured_by", "tested_to",
    }


def test_default_020_catalog_catalog_version():
    assert DEFAULT_020_CATALOG.catalog_version == "b.020-floor"


# ── load_catalog: unset / floor ---------------------------------------------


def test_load_catalog_unset_gives_020_floor(tmp_path):
    """extraction.ontology unset → the 020 floor applies (byte-for-byte
    pre-change behavior)."""
    catalog = load_catalog({})
    assert catalog == DEFAULT_020_CATALOG


def test_load_catalog_empty_string_gives_020_floor(tmp_path):
    catalog = load_catalog({"extraction.ontology": ""})
    assert catalog == DEFAULT_020_CATALOG


def test_load_catalog_missing_file_raises(tmp_path):
    """A non-empty extraction.ontology pointing at a missing file is a
    fail-fast CatalogError (run start, not per-item)."""
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(
            {"extraction.ontology": str(tmp_path / "kb.ontology.yml")})


# ── load_catalog: user catalog ------------------------------------------------


def test_load_catalog_adds_types_and_relations(tmp_path):
    """Effective catalog = 020 floor ∪ user catalog.  User entries are
    additive: they never remove the floor."""
    path = _write(tmp_path, "kb.ontology.yml", """
        catalog_version: "2026-08-24.1"
        entity_types:
          - id: building_block
            desc: "A constituent component of a product."
        relations:
          - predicate: composed_of
            from: [product, building_block]
            to: [building_block]
            desc: "a product or block is built from smaller blocks."
    """)
    catalog = load_catalog({"extraction.ontology": path})
    types = {t.id for t in catalog.entity_types}
    preds = {r.predicate for r in catalog.relations}
    assert "building_block" in types
    assert {"product", "customer", "competitor", "standard", "cert"} <= types
    assert "composed_of" in preds
    assert "competes_with" in preds  # 020 floor predicate survives


def test_load_catalog_bumped_version_stamps_b_021(tmp_path):
    """Catalog bump → effective prompt version ``b.021-<version>``;
    ``catalog_version`` recorded on the catalog itself."""
    path = _write(tmp_path, "kb.ontology.yml", """
        catalog_version: "2026-08-24.1"
        entity_types:
          - id: building_block
    """)
    catalog = load_catalog({"extraction.ontology": path})
    assert effective_prompt_version(catalog) == "b.021-2026-08-24.1"
    assert catalog.catalog_version == "2026-08-24.1"


def test_load_catalog_missing_catalog_version_is_error(tmp_path):
    """catalog_version is REQUIRED when a user catalog is given — it is
    what starts a new time-boxing period."""
    path = _write(tmp_path, "nover.yml", """
        entity_types:
          - id: building_block
    """)
    with pytest.raises(CatalogError, match="catalog_version"):
        load_catalog({"extraction.ontology": path})


def test_load_catalog_empty_file_is_floor(tmp_path):
    """An explicitly-set but empty/placeholder file is a no-op: floor."""
    path = _write(tmp_path, "empty.yml", "# no catalog entries\n")
    catalog = load_catalog({"extraction.ontology": path})
    assert catalog == DEFAULT_020_CATALOG


# ── validation (fail-fast at load time) --------------------------------------


def test_load_catalog_duplicate_entity_type_raises(tmp_path):
    path = _write(tmp_path, "dup.yml", """
        catalog_version: "1"
        entity_types:
          - id: building_block
          - id: building_block
    """)
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog({"extraction.ontology": path})


def test_load_catalog_relation_referencing_unknown_type_raises(tmp_path):
    """from/to must reference a type defined in the effective catalog
    (floor ∪ user); an undefined type → CatalogError naming it."""
    path = _write(tmp_path, "orphan.yml", """
        catalog_version: "1"
        relations:
          - predicate: composed_of
            from: [product, ghost_type]
            to: [building_block]
    """)
    with pytest.raises(CatalogError, match="ghost_type"):
        load_catalog({"extraction.ontology": path})


def test_load_catalog_malformed_yml_raises(tmp_path):
    path = _write(tmp_path, "bad.yml", "entity_types: [unclosed")
    with pytest.raises(CatalogError):
        load_catalog({"extraction.ontology": path})


def test_load_catalog_floor_relation_edges_are_unchecked(tmp_path):
    """Floor predicates (020 allowlist) have no from/to; a user relation
    whose from/to reference ONLY floor types is fine even if it
    re-declares a floor predicate id — the floor is authoritative for
    its own shape."""
    path = _write(tmp_path, "floorok.yml", """
        catalog_version: "1"
        entity_types:
          - id: building_block
        relations:
          - predicate: composed_of
            from: [product]
            to: [building_block]
    """)
    catalog = load_catalog({"extraction.ontology": path})
    preds = {r.predicate for r in catalog.relations}
    assert "composed_of" in preds and "competes_with" in preds


# ── render_domain_prompt -------------------------------------------------------


def test_render_domain_prompt_includes_types_and_predicates(tmp_path):
    path = _write(tmp_path, "kb.ontology.yml", """
        catalog_version: "1"
        entity_types:
          - id: building_block
            desc: "A constituent component of a product."
        relations:
          - predicate: composed_of
            from: [product, building_block]
            to: [building_block]
            desc: "built from smaller blocks."
    """)
    catalog = load_catalog({"extraction.ontology": path})
    prompt = render_domain_prompt(catalog)
    assert "building_block" in prompt
    assert "composed_of" in prompt
    assert "competes_with" in prompt  # 020 floor still present


def test_render_domain_prompt_floor_only(tmp_path):
    """No user catalog: floor prompt is rendered (no new types leak in)."""
    catalog = load_catalog({})
    prompt = render_domain_prompt(catalog)
    assert "building_block" not in prompt
    assert "competes_with" in prompt


def test_render_domain_prompt_includes_from_to_rules(tmp_path):
    path = _write(tmp_path, "kb.ontology.yml", """
        catalog_version: "1"
        entity_types:
          - id: building_block
        relations:
          - predicate: composed_of
            from: [product, building_block]
            to: [building_block]
            desc: "built from smaller blocks."
    """)
    catalog = load_catalog({"extraction.ontology": path})
    prompt = render_domain_prompt(catalog)
    # per-predicate from/to rules rendered inline
    assert "product" in prompt
    assert "building_block" in prompt


# ── memoization ----------------------------------------------------------------


def test_load_catalog_memoizes_per_cfg(tmp_path):
    """load_catalog(cfg) memoizes the parsed catalog on cfg, so the
    file is read once per run even if extract() is called per item."""
    path = _write(tmp_path, "kb.ontology.yml", """
        catalog_version: "9"
        entity_types:
          - id: building_block
    """)
    cfg = {"extraction.ontology": path}
    c1 = load_catalog(cfg)
    c2 = load_catalog(cfg)
    assert c1 is c2
