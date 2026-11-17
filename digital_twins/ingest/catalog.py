"""Domain extraction catalog (021-building-block-ontology).

The 020 domain profile bakes its 5 entity types and 6 predicates into
:mod:`digital_twins.ingest.entities` as constants. This change turns the
(type, predicate) catalog into user-editable data: a YAML file named by
``extraction.ontology`` (default empty) that the user can extend between
releases.

Shape (all fields optional; an empty/missing file is a no-op):

.. code-block:: yaml

   catalog_version: "2026-08-24.1"
   entity_types:
     - id: building_block
       desc: "A constituent component of a product."
   relations:
     - predicate: composed_of
       from: [product, building_block]
       to: [building_block]
       desc: "a product or block is built from smaller blocks."

The effective catalog is the **union** of the 020 floor
(:data:`DEFAULT_020_CATALOG`) and the user file. The user file is
additive-only: it can add entries but never remove the 020 floor. A
catalog entry that is not present in a user catalog merely stops *new*
extractions from emitting it; already-written nodes are governed by the
existing 020 ``supersede()`` / ``drop()`` time-box.

The catalog version is stamped like 020's ``prompt_version``:
``b.020-floor`` when the catalog equals the 020 floor exactly (020 users
keep stable edge stamps), ``b.021-<catalog_version>`` otherwise. A
catalog bump therefore starts a new time-box; older edges survive.
No ``supersede()`` / ``drop()`` change needed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Catalog",
    "CatalogError",
    "DEFAULT_020_CATALOG",
    "effective_prompt_version",
    "load_catalog",
    "render_domain_prompt",
]


class CatalogError(ValueError):
    """A catalog file failed to parse or validate (fail-fast, run start)."""


# ── dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class EntityType:
    id: str
    desc: str = ""


@dataclass(frozen=True)
class Relation:
    predicate: str
    from_types: tuple[str, ...] = ()
    to_types: tuple[str, ...] = ()
    desc: str = ""


@dataclass(frozen=True)
class Catalog:
    """Effective (type, predicate) catalog: the 020 floor ∪ user entries.

    ``catalog_version`` is the user file's ``catalog_version`` when a user
    catalog is loaded, else ``"b.020-floor"``. It is stamped onto REL
    edges alongside ``prompt_version`` (021, additive property).
    """
    entity_types: tuple[EntityType, ...] = ()
    relations: tuple[Relation, ...] = ()
    catalog_version: str = "b.020-floor"

    @property
    def type_ids(self) -> frozenset[str]:
        return frozenset(t.id for t in self.entity_types)

    @property
    def predicates(self) -> frozenset[str]:
        return frozenset(r.predicate for r in self.relations)

    def relation(self, predicate: str) -> Relation | None:
        for r in self.relations:
            if r.predicate == predicate:
                return r
        return None

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Catalog):
            return NotImplemented
        return (
            self.entity_types == other.entity_types
            and self.relations == other.relations
            and self.catalog_version == other.catalog_version
        )


# ── 020 floor ---------------------------------------------------------------

#: The 020 domain profile baked in: 5 entity types + 6 predicates.
#: The user catalog (``extraction.ontology``) is always a *superset* of
#: this floor — it can add but never remove.
DEFAULT_020_CATALOG = Catalog(
    entity_types=(
        EntityType("product"),
        EntityType("customer"),
        EntityType("competitor"),
        EntityType("standard"),
        EntityType("cert"),
    ),
    relations=(
        Relation("competes_with", ("product",), ("competitor",),
                 "a product competes with a competitor product"),
        Relation("supplied_to", ("product",), ("customer",),
                 "a product is supplied to a customer"),
        Relation("complies_with", ("product",), ("standard", "cert"),
                 "a product complies with a standard or certification"),
        Relation("substitutes_for", ("product",), ("product",),
                 "a product is a substitute for another product"),
        Relation("manufactured_by", ("product",), ("competitor",),
                 "a product is manufactured by a competitor"),
        Relation("tested_to", ("product",), ("standard",),
                 "a product is tested to a standard"),
    ),
    catalog_version="b.020-floor",
)


# ── validation ---------------------------------------------------------------


def _validate_relations(relations: list[Relation], known: set[str],
                        where: str) -> None:
    """Every from/to entry of a relation must reference a type in
    ``known`` (the effective catalog's type set). Raises
    :class:`CatalogError` naming the first offender."""
    for rel in relations:
        for t in rel.from_types:
            if t not in known:
                raise CatalogError(
                    f"{where}: relations[{rel.predicate!r}].from "
                    f"references undefined type {t!r}")
        for t in rel.to_types:
            if t not in known:
                raise CatalogError(
                    f"{where}: relations[{rel.predicate!r}].to "
                    f"references undefined type {t!r}")


# ── load ----------------------------------------------------------------------


def load_catalog(cfg: dict | None = None) -> Catalog:
    """Load + merge + validate the effective catalog for a cfg.

    * ``extraction.ontology`` unset or empty → :data:`DEFAULT_020_CATALOG`
      (the 020 floor, byte-for-byte pre-change behavior).
    * set to an existing file → the union of the 020 floor and the
      user file (additive-only).
    * set to a missing file, or a file that fails to parse / validate →
      :class:`CatalogError` (fail-fast at run start, not per-item).

    The parsed catalog is memoized on ``cfg`` so a run that calls
    :func:`digital_twins.ingest.entities.extract` per item loads the file
    once.
    """
    cfg = cfg or {}
    memo = cfg.get("_catalog_memo")
    if isinstance(memo, Catalog):
        return memo
    path = str(cfg.get("extraction.ontology") or "").strip()
    if not path:
        return DEFAULT_020_CATALOG
    p = Path(path).expanduser()
    if not p.is_file():
        raise CatalogError(
            f"extraction.ontology catalog file not found: {path}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise CatalogError(f"{path}: unparseable YAML ({e})") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CatalogError(f"{path}: top level must be a mapping")
    if not raw:
        # An explicitly-set but empty / comment-only file is a no-op:
        # the 020 floor applies (byte-for-byte pre-change behavior).
        cfg["_catalog_memo"] = DEFAULT_020_CATALOG
        return DEFAULT_020_CATALOG

    catalog_version = str(raw.get("catalog_version") or "").strip()
    if not catalog_version:
        raise CatalogError(
            f"{path}: catalog_version is required "
            "(a user catalog must carry its version; "
            "leave extraction.ontology empty to keep the 020 floor)")

    user_types: list[EntityType] = []
    seen: set[str] = set()
    for i, ent in enumerate(raw.get("entity_types") or []):
        if not isinstance(ent, dict):
            raise CatalogError(
                f"{path}: entity_types[{i}] must be a mapping")
        eid = str(ent.get("id") or "").strip()
        if not eid:
            raise CatalogError(f"{path}: entity_types[{i}].id is required")
        if eid in seen:
            raise CatalogError(
                f"{path}: duplicate entity_types[].id {eid!r}")
        seen.add(eid)
        user_types.append(EntityType(eid, str(ent.get("desc") or "").strip()))

    user_relations: list[Relation] = []
    pred_seen: set[str] = set()
    for i, rel in enumerate(raw.get("relations") or []):
        if not isinstance(rel, dict):
            raise CatalogError(
                f"{path}: relations[{i}] must be a mapping")
        pred = str(rel.get("predicate") or "").strip()
        if not pred:
            raise CatalogError(f"{path}: relations[{i}].predicate is required")
        if pred in pred_seen:
            raise CatalogError(
                f"{path}: duplicate relations[].predicate {pred!r}")
        pred_seen.add(pred)

        def _types(key: str) -> tuple[str, ...]:
            value = rel.get(key)
            if value is None:
                return ()
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (list, tuple)):
                return tuple(
                    str(v).strip() for v in value if str(v).strip())
            raise CatalogError(
                f"{path}: relations[{i}].{key} must be a list of type ids")

        user_relations.append(Relation(
            pred,
            _types("from"),
            _types("to"),
            str(rel.get("desc") or "").strip(),
        ))

    # Union with the 020 floor (additive-only: user entries are appended,
    # the floor is always present). The floor's own relations carry no
    # from/to constraints (020's flat allowlist), so only user relations
    # are validated against the effective type set.
    merged_types = DEFAULT_020_CATALOG.entity_types + tuple(user_types)
    merged_relations = (
        DEFAULT_020_CATALOG.relations + tuple(user_relations))
    effective = Catalog(
        entity_types=merged_types,
        relations=merged_relations,
        catalog_version=catalog_version,
    )
    _validate_relations(list(user_relations),
                         set(effective.type_ids), f"{path}")
    cfg["_catalog_memo"] = effective
    return effective


# ── prompt version -----------------------------------------------------------


def effective_prompt_version(catalog: Catalog) -> str:
    """Effective prompt version for a catalog: ``b.020-floor`` when the
    catalog equals the 020 floor exactly (020 users keep stable edge
    stamps), ``b.021-<catalog_version>`` otherwise (a catalog bump is a
    new time-box; older edges survive)."""
    if catalog == DEFAULT_020_CATALOG:
        return "b.020-floor"
    return f"b.021-{catalog.catalog_version}"


# ── prompt renderer ----------------------------------------------------------


def render_domain_prompt(catalog: Catalog) -> str:
    """Render the domain extraction prompt from a catalog.

    Replaces 020's constant :data:`DOMAIN_EXTRACTION_PROMPT`: the type
    list, the predicate list, and the per-predicate from/to rules are
    rendered inline so the LLM sees the *current* catalog. Floor-only
    catalogs render the 020 prompt shape (floor relations carry no
    from/to, so they render as bare allowlist entries, matching 020).
    """
    types = ", ".join(t.id for t in catalog.entity_types)
    preds = [r for r in catalog.relations]
    bare = [r for r in preds if not r.from_types and not r.to_types]
    ruled = [r for r in preds if r.from_types or r.to_types]

    lines = [
        "You are an entity-extraction pipeline for a domain knowledge base.",
        "Read the document and extract the entities and relations it contains.",
        "",
        "Rules:",
        '- "entities": domain entities that are concrete enough to be useful',
        "  for navigation. 3 to 25 entities.",
        f'- Each entity: {{"name": str, "type": one of {types},',
        '  "desc": short description, max 120 chars}}.',
        '- "relations": only relations explicitly supported by the document;',
    ]
    for r in bare:
        lines.append(
            f'   "{r.predicate}"{" — " + r.desc if r.desc else ""}')
    for r in ruled:
        frm = " or ".join(sorted(r.from_types))
        to = " or ".join(sorted(r.to_types))
        lines.append(
            f'   "{r.predicate}" (subject type: {frm}; object type: {to})'
            f'{" — " + r.desc if r.desc else ""}')
    lines += [
        '   subject and object MUST be names from "entities".',
        "- Do not invent entities or relations that are not in the document.",
        "- If the document is too thin to extract anything useful, return",
        '  {"entities": [], "relations": []}',
        "",
        "Respond with JSON only, in exactly this shape:",
        ' {"entities": [{"name": ..., "type": ..., "desc": ...}],',
        '  "relations": [{"subject": ..., "predicate": ..., "object": ...}]}',
    ]
    return "\n".join(lines)
