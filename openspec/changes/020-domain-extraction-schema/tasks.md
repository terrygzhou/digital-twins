# Tasks: 020 — Domain extraction schema (Tier B)
(Mirrors `specs/ingestion/` delta.)

## 1. Unit tests (RED-first)
- [x] 1.1 `tests/unit/test_entities.py` (extend):
      - `extract()` with `cfg={...,"domain_schema":True}` → every entity
        `type` in `DOMAIN_ENTITY_TYPES`; every relation `predicate` in
        `DOMAIN_PREDICATES`
      - domain prompt version: `extract()` returns
        `prompt_version == DOMAIN_PROMPT_VERSION`
      - a relation with a predicate **not** in `DOMAIN_PREDICATES` is
        dropped in domain mode; the same relation is kept in generic mode
      - generic profile (`domain_schema` absent/False) is **unchanged**:
        types still in the generic `ENTITY_TYPES`, free-form predicates
        kept, `prompt_version == PROMPT_VERSION`
      - `materialize()` writes `:Entity {type:'competitor'}` +
        `(si)-[REL {predicate:'competes_with'}]` for a domain extraction
      **Verification:** domain-profile tests fail before the profile exists.

## 2. Implementation
- [x] 2.1 `digital_twins/ingest/entities.py`:
      - add `DOMAIN_ENTITY_TYPES` + `DOMAIN_PREDICATES` +
        `DOMAIN_PROMPT_VERSION` + `DOMAIN_EXTRACTION_PROMPT`
      - add `_profile(cfg)` → `("domain",)` when
        `cfg.get("domain_schema")` else `("generic",)`
      - `extract()`: pick prompt + type-enum + predicate-filter by profile;
        drop out-of-set entities / out-of-allowlist relations (domain only)
      - `materialize()`: no change to write logic — it already MERGEs by
        `(name,type)` and stamps free-form `predicate`; confirm the domain
        `type` values + REL predicates flow through unchanged
      **Verification:** §1.1 tests green; generic profile byte-for-byte the
      same as pre-change.

## 3. Config knob (T027 guard)
- [x] 3.1 `extraction.domain_schema` (bool, default `false`):
      - `digital_twins/config/schema.py` `DEFAULTS` + known-key sets
      - `digital_twins/config/knobs.py` `KNOBS` entry
      - `config.example.yml` (with `# env: KB_EXTRACTION__DOMAIN_SCHEMA`)
      - `.env.example` (`#KB_EXTRACTION__DOMAIN_SCHEMA=false`)
      - `docs/configuration.md` table row
      **Verification:** `tests/unit/test_knob_docs.py` green (no
      undocumented knob).

## 4. Integration
- [x] 4.1 `tests/integration/test_neo4j_query.py` (extend, if Neo4j):
      one domain extraction, 2 `competitor`/`standard` entities + 1
      `competes_with` REL → count = 1; re-extract → still 1 (NFR-1).

## 5. Guards
- [x] 5.1 `python3 -m pytest tests/unit/test_knob_docs.py
      tests/integration/test_portability.py -q` → green.
- [x] 5.2 Full suite.

## 6. Semver + release note
- [x] 6.1 Bump `__version__` (MINOR) + CHANGELOG: "domain extraction
      profile (Tier B) behind `extraction.domain_schema`; generic profile
      unchanged; decoupled addon over Tier A."

## 7. Bookkeeping
- [x] 7.1 OpenSpec 020 artefacts validated.
- [x] 7.2 SDD ledger `020-domain-extraction-schema/progress.md`.
