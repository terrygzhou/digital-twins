# Capability: ingestion — Domain extraction schema (Tier B, delta)

## ADDED Requirements

### Requirement: A domain extraction profile is config-gated (default off)
When `extraction.domain_schema` is `true` (and `extraction.enabled` is
`true`), extraction SHALL run a domain prompt that constrains entities to
`DOMAIN_ENTITY_TYPES` (`product, customer, competitor, standard, cert`) and
relations to the `DOMAIN_PREDICATES` allowlist. When `extraction.domain_schema`
is `false` (the default), the generic profile SHALL be used unchanged.

#### Scenario: Generic profile is the default
- **WHEN** `extraction.enabled` is true and `extraction.domain_schema` is
  false or unset
- **THEN** extraction uses the generic `EXTRACTION_PROMPT`, the generic
  `ENTITY_TYPES`, free-form predicates, and `PROMPT_VERSION` — byte-for-byte
  the same as before this change (Tier A unaffected)

#### Scenario: Domain profile selected
- **WHEN** `extraction.enabled` and `extraction.domain_schema` are both true
- **THEN** extraction uses `DOMAIN_EXTRACTION_PROMPT`, `DOMAIN_ENTITY_TYPES`,
  the `DOMAIN_PREDICATES` filter, and `DOMAIN_PROMPT_VERSION`

### Requirement: Domain entities are typed, not new labels
Domain-mode entities SHALL be written as `(:Entity)` with `type` in
`DOMAIN_ENTITY_TYPES` — the S4 `(name.lower(), type)` MERGE key is the
discriminator. Domain modes SHALL NOT introduce new node labels.

#### Scenario: Domain type is a distinct MERGE key
- **WHEN** a generic run wrote `:Entity {name:'Rotork', type:'organization'}`
  and a domain run writes `:Entity {name:'Rotork', type:'competitor'}`
- **THEN** they are two distinct nodes (different `type`) and the domain run
  does not overwrite the generic one

#### Scenario: Out-of-set domain entity discarded
- **WHEN** the LLM returns an entity with `type: 'material'` (not in
  `DOMAIN_ENTITY_TYPES`) in domain mode
- **THEN** that entity is discarded, not written

### Requirement: Domain relations are predicate-constrained
In domain mode, a relation whose `predicate` is not in `DOMAIN_PREDICATES`
(`competes_with, supplied_to, complies_with, substitutes_for,
manufactured_by, tested_to`) SHALL be dropped by `extract()`. The prompt
instructs domain predicates; the filter is defense in depth. In generic mode
predicates remain free-form.

#### Scenario: Non-domain predicate dropped
- **WHEN** domain mode and the LLM returns a relation with
  `predicate: 'related_to'` (not in the allowlist)
- **THEN** that relation is dropped; a `competes_with` relation is kept and
  materialized as `(si)-[:REL {predicate:'competes_with'}]`

### Requirement: Profiles coexist via prompt_version (clean decoupling)
Domain extractions SHALL be stamped `DOMAIN_PROMPT_VERSION`; generic
extractions `PROMPT_VERSION`. `supersede()` and `drop()` SHALL time-box and
prune per **item** (`item_id` / `r.source_item`): a re-extraction of the same
item in the other profile stamps `valid_to` on that item's edges of **both**
profiles (the time-box is per-item, a pre-existing 018 limitation), while a
different item's edges are untouched. For distinct items, B's re-extraction
SHALL NOT mutate or remove A's edges, and vice versa.

#### Scenario: Re-running B leaves A's edges live
- **WHEN** an item is extracted in the generic profile and a **different**
  item is extracted in the domain profile
- **THEN** each item's `MENTIONED`/`REL` edges are time-boxed to that item's
  own `source_item`; the domain pass writes its own version-stamped edges
  and does not delete the generic item's edges

#### Scenario: Same item re-extracted in the other profile
- **WHEN** an item is extracted in the generic profile then the **same** item
  is re-extracted in the domain profile
- **THEN** `supersede(item_id)` stamps `valid_to` on that item's MENTIONED
  and REL edges regardless of profile; the domain pass writes its own
  version-stamped edges; no edges of *other* items are mutated

### Requirement: Pinned prompt_version override wins in both profiles
When `extraction.prompt_version` is pinned, it SHALL be stamped on
extractions in BOTH profiles — the pinned value overrides
`PROMPT_VERSION` (generic) and `DOMAIN_PROMPT_VERSION` (domain) alike.
An operator who pins a generic version string while enabling the domain
profile stamps generic-version edges; a re-extraction with the same
pinned value supersedes both profiles' edges of that item.

## Non-goals
No new node labels; no generic-profile change; no `supersede()`/`drop()`
semantic change; no second LLM endpoint; no cross-channel / cross-item REL.
