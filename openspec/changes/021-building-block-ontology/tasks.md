# Tasks: 021 — Building-block ontology (dynamic catalog)
(Mirrors `specs/ingestion/` delta.)

## 1. Unit tests (RED-first)
- [ ] 1.1 `tests/unit/test_catalog.py` (new):
      - `load_catalog(cfg={})` → returns the 020 floor
        (5 types, 6 predicates), `catalog_version == "b.020-floor"`
      - `load_catalog` with a user catalog (via a tmp `kb.ontology.yml`)
        → union of 020 floor + user entries; user entries never remove
        the floor
      - duplicate `entity_types[].id` → `CatalogError`
      - `relations[].predicate` with `from`/`to` referencing an
        undefined type → `CatalogError`
      - missing catalog file (non-empty `extraction.ontology`) →
        `CatalogError` (fail-fast at run start, not per-item)
      - empty catalog file / missing `extraction.ontology` → 020 floor
        (byte-for-byte identical to pre-change behavior)
      - prompt-version stamp:
        - 020 floor → `b.020-floor`
        - catalog bump → `b.021-<version>`
      - rendered prompt contains every catalog type + predicate
      **Verification:** catalog tests fail before the loader exists.

- [ ] 1.2 `tests/unit/test_entities.py` (extend):
      - `extract()` in domain mode with a user catalog → entity types
        and relation predicates constrained to the *effective*
        catalog (020 floor ∪ user); `prompt_version` stamped
        `b.021-<version>`
      - a relation whose `subject_type` is not in `pred.from` (or
        `object_type` not in `pred.to`) is dropped in domain mode;
        the same relation is kept in generic mode
      - `materialize()` on a catalog-driven extraction writes
        `:REL {catalog_version: ...}` alongside `prompt_version`
      - generic profile (no catalog, no `domain_schema`) is
        **unchanged**: types still in `ENTITY_TYPES`, free-form
        predicates kept, `prompt_version == PROMPT_VERSION`
      **Verification:** §1.1/§1.2 catalog tests fail before the
      catalog-driven `extract()` exists.

## 2. Implementation
- [ ] 2.1 New `digital_twins/ingest/catalog.py`:
      - `DEFAULT_020_CATALOG` (the 020 floor: types + predicates)
      - `load_catalog(cfg) -> Catalog` (load + merge + validate)
      - `render_domain_prompt(catalog) -> str`
      - `effective_prompt_version(catalog) -> str`
      - `CatalogError` (fail-fast, maps to run-level `failed`)
      **Verification:** §1.1 green; 020 floor behavior byte-for-byte
      the same as 020.

- [ ] 2.2 `digital_twins/ingest/entities.py`:
      - `extract()`: resolve the effective catalog; render the prompt;
        constrain `allowed_types` / `allowed_predicates` /
        `predicate_edges` to the catalog; stamp `catalog_version`
        alongside `prompt_version`
      - `materialize()`: stamp `:REL {catalog_version}` (additive
        property, no change to MERGE key or write logic)
      **Verification:** §1.2 green; 020-only path (no user catalog)
      produces identical graph writes to 020.

- [ ] 2.3 `digital_twins/ingest/pipeline.py`:
      - load the catalog once per run (before the fan-out loop);
        surface `CatalogError` as a run-level `failed` status (not
        per-item)

## 3. Config knob (T027 guard)
- [ ] 3.1 `extraction.ontology` (str, default `""`):
      - `digital_twins/config/schema.py` `DEFAULTS` + known-key sets
      - `digital_twins/config/knobs.py` `KNOBS` entry
      - `config.example.yml` (with `# env: KB_EXTRACTION__ONTOLOGY`)
      - `.env.example` (`#KB_EXTRACTION__ONTOLOGY=`)
      - `docs/configuration.md` table row
      **Verification:** `tests/unit/test_knob_docs.py` green.

- [ ] 3.2 New `kb.ontology.example.yml` (shipped, in the repo root
      alongside `config.example.yml`): the 5-layer building-block
      catalog from `design.md` §shipped-default.

## 4. Integration
- [ ] 4.1 `tests/integration/test_neo4j_query.py` (extend, if Neo4j):
      - one catalog-driven domain extraction (user catalog with
        `building_block`, `capability`, `value_stream`, `motivation`)
        → entity count + REL count + `catalog_version` property
        present; re-extract → counts stable (NFR-1)

## 5. Guards
- [ ] 5.1 `python3 -m pytest tests/unit/test_knob_docs.py
      tests/integration/test_portability.py -q` → green.
- [ ] 5.2 Full suite.

## 6. Semver + release note
- [ ] 6.1 Bump `__version__` (MINOR) + CHANGELOG: "building-block
      ontology (021) — user-editable `kb.ontology.yml` catalog extends
      the 020 domain profile; 020 behavior unchanged when the knob is
      unset."

## 7. Bookkeeping
- [ ] 7.1 OpenSpec 021 artefacts validated.
- [ ] 7.2 SDD ledger `021-building-block-ontology/progress.md`.

## Follow-ups (out of scope, tracked here for continuity)
- F1. LLM-driven catalog evolution: an agent writes new blocks to
  `kb.ontology.yml` between releases. The catalog file is the stable
  contract that makes this feasible; the write path is a separate
  change (likely `022-catalog-evolution`).
- F2. Auto-sweep of a corpus under a bumped catalog: the operator runs
  the existing ingest / heal machinery; the catalog is just a richer
  prompt + filter.
