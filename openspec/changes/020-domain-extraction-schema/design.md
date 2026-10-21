# Design: 020 Domain extraction schema (Tier B)

## Context
018 already ships a `:REL {predicate}` edge where `predicate` is a free-form
snake_case string — no per-predicate Cypher. And S4's `:Entity` MERGE key is
`(name.lower(), type)`. Both facts mean the "domain schema" needs **no graph
schema change**: the domain is expressed entirely through (a) which `type`
values the prompt emits and (b) which `predicate` strings the prompt emits.
The graph write path (`materialize()` Step 3 MENTIONED + Step 4 REL) is
reused unchanged; only the prompt, the allowed-type set, and the predicate
filter differ.

## Decisions
1. **`type` is the schema, not a label.** Domain entities are written as
   `:Entity {type: 'product'|'customer'|'competitor'|'standard'|'cert'}`.
   A `competitor` and a generic `organization` that share a `name` are two
   *distinct* MERGE keys (`(name, competitor)` vs `(name, organization)`), so
   Tier B never overwrites or merges into Tier A's generic entities even when
   the surface name matches. Cross-profile queries are possible because both
   profiles share the `:SourceItem` / `item_id` spine and `channel`.
2. **Predicate allowlist over free-form.** In domain mode the prompt is
   instructed to emit only `DOMAIN_PREDICATES`; `extract()` additionally
   drops any relation whose `predicate` is not in the allowlist (defense in
   depth — the LLM may drift). Generic mode keeps free-form predicates
   (018 behaviour).
3. **One profile knob, not a per-type switch.** `extraction.domain_schema`
   (bool, default `false`) selects the whole domain profile atomically:
   prompt + type set + predicate filter together. No half-state where the
   prompt emits domain types but the filter only knows generic ones.
4. **Version-stamped profile → clean coexistence.** Domain runs use
   `DOMAIN_PROMPT_VERSION`; the generic profile keeps `PROMPT_VERSION`.
   `supersede()` / `drop()` already scope by `prompt_version`, so a B
   re-extraction time-boxes only B's edges; A's edges survive. This is what
   makes B "runnable in isolation without impacting A" true at the data
   level, not just the code level.
5. **Prompt reuses the LLM endpoint.** No new endpoint knob. `DOMAIN_
   EXTRACTION_PROMPT` goes through the same `_llm_request` (JSON mode,
   `temperature 0.0`, `max_tokens 3000`).

## The two sub-modes of "B over A"
- **(a) Independent typed layer (this change, recommended).** B's nodes +
  typed REL edges are orthogonal; joined to A only by `item_id` / `channel`.
  B is fully droppable; A stays a working generic mention-graph regardless.
- **(b) Refinement/lift (out of scope).** Re-typing an A entity into a B
  type would couple B to A's specific node output and break isolation.
  Not what this change does; if wanted later it is a separate change that
  migrates MERGE keys.

## Non-goals
No new node labels; no generic-profile or `supersede()`/`drop()` change;
no second LLM endpoint; no cross-channel/cross-item REL; no auto-sweep.
