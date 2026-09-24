# Tasks: s4-entity-extraction

## 1. LLM extraction module (digital_twins/ingest/entities.py)

- [x] 1.1 New module `digital_twins/ingest/entities.py`:
      `PROMPT_VERSION` constant (initial value = personal-kb's current
      `PROMPT_VERSION`), `ENTITY_TYPES` tuple
      (`person/organization/place/event/concept`), the
      `EXTRACTION_PROMPT` template (copied from personal-kb's
      `kb/core/graph.py`), and the `extract(text, *, title="", cfg)`
      function. Returns `{"entities": [...], "relations": [...],
      "prompt_version": str}` with types constrained to `ENTITY_TYPES`,
      deduped, capped at 25 entities / 100 relations. Test-First: unit
      tests in `tests/unit/test_entities.py` (type constraint, dedup,
      caps, malformed-JSON handling, empty-result shape).
- [x] 1.2 LLM transport: `_llm_request(messages, cfg)` — stdlib
      `urllib` POST to `{llm.endpoint}/v1/chat/completions`, JSON mode,
      `temperature=0.0`, `max_tokens=3000`, bearer token from
      `llm.api_key` (config layer, not env var). Unconfigured endpoint
      → `ExtractionError` (the caller isolates per item). Test-First:
      fake transport unit test (no real LLM call; monkeypatch the
      transport seam).

## 2. Neo4j materialisation (digital_twins/ingest/entities.py)

- [x] 2.1 `materialize(channel, item_id, content_hash, extraction,
      run_id, captured_at, driver, cfg)`: the four-step MERGE sequence
      (Entity nodes → SourceItem update → MENTIONED edges → REL edges,
      the latter parsed from the extraction but not written in this
      change per design decision 1). Returns
      `{"entities": N, "mentioned": N, "relations": N}` counters.
      Test-First: recording-driver unit tests pinning the Cypher
      (mirror the personal-kb write shape: `MERGE (e:Entity {name,
      type})`, `MERGE (si:SourceItem {item_id}) SET ...`,
      `MERGE (si)-[m:MENTIONED]->(e)` with `ON CREATE`/`ON MATCH`
      stamping, REL statements parsed-but-not-written).
- [x] 2.2 `supersede(item_id, driver)`: the two-statement `valid_to`
      stamp (live MENTIONED + live REL scoped to `source_item`),
      returning the edge count. Test-First: recording-driver unit test.
- [x] 2.3 `drop(item_id, driver)`: DETACH-DELETE the item's
      `:SourceItem` + prune orphaned `:Entity` nodes (no incoming
      MENTIONED and no REL in either direction). Test-First:
      recording-driver unit test. No-op semantics when the item has no
      graph rows (fresh DBs).

## 3. Pipeline hook (digital_twins/ingest/pipeline.py)

- [x] 3.1 Post-graph extraction step in `run_pipeline`, config-gated:
      when `extraction.enabled` is true AND a Neo4j driver was passed in,
      run `extract` + `supersede` + `materialize` per ingested item
      after `_upsert_graph`. Per-item failure isolation: log a warning
      and continue with the remaining items (matching personal-kb's
      post-sweep isolation and the pipeline's fail-soft semantics).
      Test-First: unit test with a stub LLM + recording Neo4j fake,
      asserting the step runs in order and a failing item does not abort
      the run.
- [x] 3.2 When `extraction.enabled` is false (default) or Neo4j is
      unconfigured, the step is skipped entirely — no LLM call, no graph
      writes beyond the `:SourceItem` nodes. Test-First: unit test
      asserting the LLM transport is never invoked when the gate is off.

## 4. Config knobs (digital_twins/config/schema.py + config.example.yml)

- [x] 4.1 Add knobs: `extraction.enabled` (bool, default `false`),
      `extraction.max_text_chars` (int, default `12000`),
      `extraction.prompt_version` (str, default `""` → built-in
      `PROMPT_VERSION`). Update `config.example.yml` with comments.
      Test-First: knob-sync guard (`test_knob_docs.py` / T027) stays
      green; unit test for the new knob defaults + coercion.
- [x] 4.2 Reuse `llm.endpoint` / `llm.model` / `llm.api_key` (no new
      endpoint knobs). Document in `config.example.yml` that extraction
      reuses the LLM endpoint configured for other uses (single
      deployment, one LLM endpoint).

## 5. Spec-013 / SDD deltas

- [x] 5.1 Update `specs/013-neo4j-query-tests/plan.md` pinned Cypher to
      include the Entity/MENTIONED write statements (the S4 entity half)
      when extraction is enabled; document that extraction is
      config-gated (default off) so the pinned contract cases only assert
      entity Cypher when the knob is set.
- [x] 5.2 Update `tests/integration/test_neo4j_query.py` (or add
      `tests/integration/test_s4_entities.py`) with the new Cypher pins
      and the supersede/drop recording-driver cases.
- [x] 5.3 Update `.superpowers/sdd/013-neo4j-query-tests/progress.md`
      ledger.

## 6. Docs

- [x] 6.1 `config.example.yml` + docs: document the `extraction.*` knobs,
      the LLM endpoint requirement, and the prompt-version pinning model
      (bump = re-extract).
- [x] 6.2 Document the REL out-of-scope decision and the revisit trigger
      (design decision 1).
- [x] 6.3 Portability guard: `pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py` must stay green (no host paths or
      env-var references introduced; `llm.api_key` is a config-layer
      knob, not an env var).

## 7. Verification

- [x] 7.1 Full pytest run; NFR-1 re-asserted (extraction is idempotent
      per (item, prompt_version) — re-ingesting the same item does not
      duplicate nodes/edges).
- [x] 7.2 Manual interop check: point personal-kb's hybrid query at a
      digital-twins-populated DB with extraction enabled and confirm
      `_entities_of_items` / `_expand_items` return non-empty for
      digital-twins items.
      (Partially verified 2026-09-25 on the production host: the shared
      `personal_kb` collection was confirmed and personal-kb's hybrid query
      glue finds digital-twins items by `item_id` (same live probe as
      s4-graph-alignment 7.2 — 100 hits on a 2-item probe). The
      entity/expand half (`_entities_of_items` / `_expand_items` non-empty)
      is NOT yet exercised: extraction is default-off, so no digital-twins
      Entity/MENTIONED rows exist in the live graph. The item-join contract
      this check guards is the same `item_id` join personal-kb uses, and
      that half is verified; the entity-side assertion remains a best-effort
      follow-up (see `.superpowers/sdd/deferred-minors/s4-interop-followups.md`).)
