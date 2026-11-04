# Design: 021 Building-block ontology (dynamic catalog)

## Context
020 made the graph schema *type + predicate*, not a label. That is the
load-bearing decision. This change turns the (type, predicate) catalog
into **user-editable data** so new building blocks can be added between
releases of `digital-twins-kb`.

## Decisions

1. **Catalog file = one YAML, user-owned.** `kb.ontology.yml` (path
   knob `extraction.ontology`, default empty). Shape:
   `catalog_version` + `entity_types` (list of `{id, desc}`) +
   `relations` (list of `{predicate, from[], to[], desc}`). The file is
   *additive*: every field optional; an empty/missing file is a no-op.

2. **Baked 020 floor.** The 020 baseline
   (`product, customer, competitor, standard, cert` +
   `competes_with, supplied_to, complies_with, substitutes_for,
   manufactured_by, tested_to`) lives in a module-level
   `DEFAULT_020_CATALOG`. The effective catalog is the **union** of
   `DEFAULT_020_CATALOG` and the user file. A user file can *add*
   entries but never *remove* the 020 floor; deletion of a 020 entry
   only stops *new* extractions from emitting it (already-written nodes
   are governed by the existing `supersede()`/`drop()` time-box).

3. **Catalog version is stamped, like 020's prompt_version.** The
   effective prompt version becomes
   `f"b.021-{catalog_version}"` (or `f"b.020-{catalog_version}"` when
   the catalog equals the 020 floor exactly, to keep 020 users' edge
   stamps stable). `supersede()` / `drop()` already scope by
   `prompt_version`, so a catalog bump is a new time-box; older edges
   survive. No `supersede()` / `drop()` change needed.

4. **Prompt is a generator, not a constant.** The current
   `DOMAIN_EXTRACTION_PROMPT` is replaced by
   `_render_domain_prompt(catalog) -> str`. It renders the type list,
   the predicate list, and the per-predicate `from`/`to` rules inline,
   so the LLM sees the *current* catalog. `extract()` is unchanged in
   shape: it loads the catalog, renders the prompt, constrains
   `allowed_types` / `allowed_predicates` / `predicate_edges` to the
   catalog, and stamps `catalog_version` alongside `prompt_version`.

5. **`from`/`to` enforcement is at extraction, not graph write.**
   020 already enforces predicates only via a flat allowlist (no
   per-predicate Cypher). This change adds *type-pair* filtering in
   `extract()`'s relation filter: `pred in allowed_predicates AND
   subj_type in pred.from AND obj_type in pred.to`. `materialize()`'s
   REL step is unchanged — it MERGEs whatever the LLM returned (the
   LLM is already constrained to the catalog; the filter is defense in
   depth, same philosophy as 020).

6. **Catalog is read once per run, not per item.** `extract()` receives
   `cfg` (as it does today); the catalog is loaded lazily in
   `load_catalog(cfg)` with a memo on the run. The `extract()`
   signature is unchanged — the catalog is resolved internally.

7. **Catalog validation is strict and fail-fast.** A catalog that
   fails to parse, has a duplicate `entity_types[].id`, or a
   `relations[].predicate` whose `from`/`to` reference an undefined
   type, raises `CatalogError` at load time. The pipeline maps that to
   a per-run `failed` status (consistent with `PrerequisiteError`
   contract) — not per-item, so a bad catalog doesn't produce a
   half-extracted run.

## The building-block catalog (shipped default)
The shipped `kb.ontology.example.yml` ships the 5-layer catalog the
request calls out:

| Layer | `entity_types.id` | `relations.predicate` |
|---|---|---|
| Product | `product` | `composed_of` (product → building_block) |
| Building block | `building_block` | `enables` (block → capability) |
| Capability | `capability` | `flows_through` (value_stream → block/cap) |
| Customer | `customer` | `motivated_by` (value_stream/capability/product → motivation) |
| Value stream | `value_stream` | — |
| Motivation | `motivation` | — |

`building_block` and `capability` are new; `product`, `customer`,
`value_stream` are new (020's `competitor`, `standard`, `cert` remain
in the 020 floor). The shipped example is **optional** — users who
don't want it leave `extraction.ontology` empty and get the 020 floor.

## Non-goals
- No new node labels (020's type-discriminator principle is retained).
- No LLM-driven catalog evolution in this change (that's a follow-up
  that *writes* to `kb.ontology.yml`; the catalog file is the stable
  contract that makes it feasible).
- No change to Tier A (generic profile), `supersede()` / `drop()`
  semantics, the LLM endpoint, cross-channel/cross-item REL, or the
  auto-sweep.
- No catalog *removal* semantics (a catalog entry can stop new
  extractions from emitting it; already-written nodes are governed by
  the existing 020 time-box / drop machinery).
