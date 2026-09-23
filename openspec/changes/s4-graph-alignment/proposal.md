# Change: S4 structural graph alignment

## Why
digital-twins writes its own minimal Neo4j graph (`KbItem`/`KbChunk` + `HAS_CHUNK`)
and a Qdrant payload (`source`, `source_url`, `item_key`, `text`, `ts`) that
personal-kb's S4/ADR-003 query layer cannot join: `_detect_schema` keys off
`:SourceItem`, and `_point_hits` scrolls Qdrant on the `item_id` payload field
which digital-twins does not emit. To share one Neo4j DB and one Qdrant
collection, digital-twins must emit S4's `:SourceItem` node and S4-compatible
payload fields.

## What changes
- **Neo4j**: replace `_upsert_graph`'s `KbItem`/`KbChunk`/`HAS_CHUNK` with a
  single `MERGE (si:SourceItem {item_id})` per item, setting
  `channel`, `content_hash` (SHA-256 of item text). Drop chunk nodes.
- **Qdrant payload**: add `item_id`, `content_hash`, `content_snippet`,
  `full_content`, `captured_at`, `source_title`, `total_chunks`,
  `embed_model`, `source_type`, `tags` (existing `source_url` stays); keep
  `owner`/`owner_tag` moved under `meta`; optional provenance passthroughs
  `run_id`/`trigger` (not contract fields; when present must equal the
  audit row's values).
- **Point ID**: switch to content-independent `uuid5(NAMESPACE_DNS,
  "kb:{channel}:{item_id}:{chunk_index}")` (personal-kb `kb/core/ids.py`
  convention) so re-ingest upserts in place (BR-6.1 parity) instead of minting
  a new ID when content changes.
- **Spec-013 test contract**: update pinned Cypher to the S4 shape.
- Entity/`MENTIONED`/`REL` extraction (LLM) is explicitly out of scope here.

## Impact
- Affected specs: 013-neo4j-query-tests (test contract delta).
- NFR-1 dedup is scoped to within-system; cross-system content dedup requires
  a channel-mapping table (see design.md "Cross-system collision").
- Affected code: `digital_twins/ingest/ids.py`, `digital_twins/ingest/pipeline.py`,
  Neo4j migration/health checks, spec-013 tests, docs.
- Existing digital-twins Qdrant points: point-ID scheme change means old points
  are orphans — migration decision recorded in Non-goals (delete + re-ingest is
  the MVP answer; no in-place rewrite of historical points).

## Non-goals
- No LLM entity extraction (`Entity`/`REL`, `PROMPT_VERSION`, time-boxing,
  supersede/drop) — separate follow-up change (s4-entity-extraction).
- No in-place migration of historical Qdrant points (re-ingest only).
- No changes to personal-kb itself (read-only reference).
- No new host paths / dependencies (NFR-13 / BR-11); `embed_model` value comes
  from the config layer.
