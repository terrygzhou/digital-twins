# Change: 020 — Domain extraction schema (Tier B)

## Why
`entities.py` (s4-entity-extraction + 018) ships a single *generic* prompt
constrained to `ENTITY_TYPES = (person, organization, place, event, concept)`
and free-form `predicate` strings. Against a B2B valve-catalog corpus (KEYVA),
that generic profile under-captures the domain: it labels `API 598`, `Inconel
625`, `Rotork`, `VALIN` as loose `concept`/`organization` and invents no
`competes_with` / `supplied_to` / `complies_with` structure. The Tier-A graph
is proven and working; Tier B adds the domain layer **without touching Tier A**.

## What changes
- `digital_twins/ingest/entities.py`:
  - New `DOMAIN_ENTITY_TYPES`
    (`product, customer, competitor, standard, cert`) — the domain
    discriminator. Because S4 MERGEs `:Entity` by `(name.lower(), type)`,
    the domain "schema" is the *`type` value* (+ predicate), **not** a new
    node label. `materialize()`/`supersede()`/`drop()` are reused as-is.
  - New `DOMAIN_PREDICATES` allowlist
    (`competes_with, supplied_to, complies_with, substitutes_for,
    manufactured_by, tested_to`) + `DOMAIN_EXTRACTION_PROMPT`
    (`DOMAIN_PROMPT_VERSION`) that emits the 5 domain types and only the
    domain predicates.
  - A profile selector: when `extraction.domain_schema` is true,
    `extract()`/`materialize()` use the domain prompt, the union
    `ENTITY_TYPES + DOMAIN_ENTITY_TYPES`, and the `DOMAIN_PREDICATES`
    filter (non-domain relations dropped). The generic profile (default) is
    byte-for-byte unchanged.
- **New knob:** `extraction.domain_schema` (bool, default `false`).
  Thru schema.py `DEFAULTS`, `KNOBS` registry, `config.example.yml`,
  `.env.example`, `docs/configuration.md` (T027 knob-doc guard).

## How it stays decoupled from Tier A
Tier B is a **separate prompt profile**, opt-in via the knob, that runs the
same `entities.py` machinery. Generic (Tier A) writes are stamped
`prompt_version = PROMPT_VERSION`; a domain re-extraction is stamped
`DOMAIN_PROMPT_VERSION`. `supersede()` / `drop()` already time-box and
prune **per `prompt_version`**, so the two profiles coexist and
cross-time-box cleanly — re-running B never re-runs or mutates A's nodes,
and B can be turned off with zero effect on A's already-written graph.
Tier B is a new LLM pass, new `type` values, new predicates, same spine
(`:SourceItem` / `item_id`) — an orthogonal enrichment, not a rework.

## Impact
- `digital_twins/ingest/entities.py` (types + prompt + selector + filter)
- `digital_twins/config/{schema,knobs}.py` + `config.example.yml`,
  `.env.example`, `docs/configuration.md` (the one new knob)
- `tests/unit/test_entities.py` (domain profile) + `test_knob_docs.py`
- Semver: MINOR (additive; gated by a new default-off knob)

## Non-goals
- No new Neo4j node labels (S4 `name+type` MERGE-dedup is the discriminator;
  domain structure lives in `type` + `predicate`).
- No change to the generic profile, `supersede()` / `drop()` semantics,
  personal-kb, or cross-channel / cross-item REL.
- No second LLM endpoint — the domain profile reuses `llm.endpoint` /
  `llm.model` / `llm.api_key`.
- No auto-ingest of the full corpus in this change (the sweep is an
  operator action; see the separate full-corpus run).
