# Design: 018 REL edge materialization

## Context
`entities.py` is already half-wired for REL:
- `extract()` parses `relations` (subject/predicate/object, both ends
  constrained to extracted entities).
- `supersede()` already stamps `valid_to` on live REL edges
  (`MATCH ()-[r:REL]->() WHERE r.source_item = $item_id`).
- `drop()`'s orphan prune already checks REL references before
  deleting an entity.
Only `materialize()` Step 4 is a no-op. This change fills that one gap.

## Decisions
1. **Mirror MENTIONED edge logic.** The REL MERGE's ON-CREATE /
   ON-MATCH clauses are the same shape as the MENTIONED step:
   ON-CREATE sets `captured_at`/`run_id`/`prompt_version`/`valid_from`;
   ON-MATCH clears `valid_to`, bumps `last_seen`/`run_id`. Consistency
   keeps supersede/drop/re-extract behaviour uniform across both edge
   types.
2. **Same gate, no new knob.** REL writes happen iff
   `extraction.enabled` and a driver exist — the exact gate that runs
   `supersede`/`extract`/`materialize` today. No new config.
3. **Relation filtering already done upstream.** `extract()` drops any
   relation whose subject or object is not in the extracted entity
   set, so `materialize()` can MERGE every relation in the parsed list
   without re-validating ends.
4. **Return counter, not a bool.** `materialize()` returns
   `relations: M` (write counter, 0 on a no-op re-run) so the pipeline
   can report REL work the same way it reports `entities` /
   `mentioned`.

## Non-goals
No cross-item / cross-channel REL; no prompt or schema change; no new
knob; `supersede()`/`drop()`/`extract()` unchanged.
