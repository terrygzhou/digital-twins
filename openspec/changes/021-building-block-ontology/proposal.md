# Change: 021 — Building-block ontology (dynamic catalog, Tier B extension)

## Why
020 (Tier B domain extraction) proved the right extension pattern: the
graph's *schema* lives in the `type` value of `:Entity` plus the `predicate`
value of `:REL`, **not** in a node label. That decision is the hook this
change rides on.

020 still hardcodes the catalog in `entities.py`
(`DOMAIN_ENTITY_TYPES = (product, customer, competitor, standard, cert)`
and `DOMAIN_PREDICATES = (competes_with, supplied_to, complies_with,
substitutes_for, manufactured_by, tested_to)`). Adding a new building
block — say `capability`, `building_block`, `value_stream`, or
`motivation` — requires a **code edit + version bump + re-extract**.

The request is for a *continuously extensible* ontology: the user (or a
tool, or an LLM that proposes new blocks) should be able to add blocks to
the catalog **without touching `digital_twins/` source**.

## What changes
1. **A versioned, user-editable ontology catalog file** at
   `kb.ontology.yml` (path resolved by the config layer; default
   `<user_config>/ontology.yml`). The catalog is pure data:

   ```yaml
   # kb.ontology.yml — Tier B building-block ontology
   catalog_version: 2026-08-24.1
   entity_types:
     - { id: product,        desc: "A named offering or system" }
     - { id: capability,    desc: "A thing a product/customer can do" }
     - { id: building_block, desc: "A constituent component of a product" }
     - { id: value_stream,  desc: "An ordered value-producing process" }
     - { id: motivation,    desc: "A 'why' — what drives a value stream" }
   relations:
     - { predicate: composed_of,
         from: [product, building_block],
         to:   [building_block],
         desc: "a product or block is built from smaller blocks" }
     - { predicate: enables,
         from: [capability, building_block],
         to:   [capability, value_stream],
         desc: "a capability or block makes another possible" }
     - { predicate: flows_through,
         from: [value_stream],
         to:   [value_stream, building_block, capability],
         desc: "value moves along the stream through blocks/caps" }
     - { predicate: motivated_by,
         from: [value_stream, capability, product],
         to:   [motivation],
         desc: "the 'why' layer — what this block is for" }
   ```

   The file is **additive**: every field is optional; an empty file is
   the shipped default and behaves identically to 020.

2. **`entities.py` becomes catalog-driven.** Instead of two module-level
   tuples, a single `load_catalog(cfg)` function resolves
   "builtin 020 catalog ∪ user catalog" and returns:
   - `allowed_types` — the union of the 020 baseline and any
     `catalog.entity_types[].id`;
   - `allowed_predicates` — union of the 020 baseline and any
     `catalog.relations[].predicate`;
   - `predicate_edges` — `from`/`to` type pairs (enforced at
     extraction, not graph write; mirrors 020's "defense in depth");
   - `catalog_version` — stamped on `:REL {catalog_version}` alongside
     `prompt_version` (so a catalog revision re-extracts cleanly without
     forcing a prompt revision; and vice versa).

   The generic (Tier A) profile is **byte-for-byte unchanged**. The
   020 baseline is baked into the catalog loader as a `DEFAULT_020`
   tuple so existing 020 users see no behavior change.

3. **New knob: `extraction.ontology` (str, default `""`)** — path to the
   catalog file, resolved through the config layer. Empty string = "use
   the built-in 020 catalog, ignore user file". A non-empty path with a
   missing file → `PrerequisiteError`-style fail-fast at ingest start
   (consistent with `pipeline.py`'s prerequisite contract).

4. **`extract()` prompt becomes a template.** The 020
   `DOMAIN_EXTRACTION_PROMPT` becomes a *generator*: given a catalog it
   renders the type list + predicate list + per-predicate `from`/`to`
   rules inline, so the LLM sees the **current** catalog and the
   extraction is constrained to it. The prompt version becomes
   `f"b.021-{catalog_version}"` so a catalog revision auto-stamps the
   new version and old edges survive (`supersede()` already scopes by
   `prompt_version` — no change needed).

5. **New graph write (optional, catalog-driven).** A `catalog_version`
   property on `:REL` (and a `:Entity` `catalog_version` if the entity
   type came from a user catalog) so queries can time-box "which
   catalog revision produced this edge" without a full re-extract.

## How it stays decoupled from 020
- **020 is a *floor*, not a ceiling.** Every 020 type/predicate remains
  legal when the user catalog is empty or omits it; the user catalog
  only adds, never removes. Removing a 020 entry from the catalog does
  *not* delete already-materialized nodes/edges — it only stops *new*
  extractions from emitting them. Re-extract after removal cleans up
  via the existing `supersede()` / `drop()` path.
- **Catalog revision is version-stamped, like 020's `prompt_version`.**
  A catalog bump is a new time-box; older edges survive. Same
  coexistence guarantee 020 gave "B over A", now applied inside B
  itself.
- **Catalog is data, not code.** The user adds blocks by editing one
  YAML file + bumping `catalog_version`. No Python, no re-publishing of
  the package, no LLM prompt edit (the prompt is rendered from the
  catalog). This is what makes "dynamic addition" real: a user, an
  LLM agent, or a CI job can extend the ontology between releases.

## Impact
- `digital_twins/ingest/entities.py` (catalog loader + prompt
  generator + `catalog_version` stamp + optional `catalog_version`
  property on `:REL`)
- `digital_twins/config/{schema,knobs}.py` + `config.example.yml`,
  `.env.example`, `docs/configuration.md` (the one new knob
  `extraction.ontology`; T027 knob-doc guard)
- New `digital_twins/ingest/catalog.py` (or `entities.py` extension)
  — catalog load + merge + validate + render-prompt
- `tests/unit/test_entities.py` + new `tests/unit/test_catalog.py`
- Semver: MINOR (additive; gated by a new default-empty knob; 020
  behavior unchanged when the knob is unset)

## Non-goals
- **No new Neo4j node labels.** The 020 discriminator principle is
  retained: a `capability` and a `product` that share a `name` are two
  distinct MERGE keys (`(name, capability)` vs `(name, product)`).
- **No automatic LLM-proposed catalog evolution in this change.** The
  catalog is *user-authored data*; an LLM-driven "propose new blocks"
  loop is a separate follow-up that writes to `kb.ontology.yml` (the
  catalog file being the stable contract is what makes that follow-up
  feasible).
- **No changes to Tier A (generic profile), `supersede()` / `drop()`
  semantics, the LLM endpoint, or cross-channel/cross-item REL.**
- **No auto-sweep of a corpus.** The operator runs the existing
  ingest / heal machinery; the catalog is just a richer prompt +
  filter.
