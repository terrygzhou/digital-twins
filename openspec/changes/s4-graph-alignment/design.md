# Design: S4 structural alignment

## Context
personal-kb's S4/ADR-003 graph (reference: `kb/core/graph.py` `materialize()`):

- `(:SourceItem {item_id, channel, content_hash})`
- `(:Entity {name, type, created, desc})`
- `(:SourceItem)-[:MENTIONED {run_id, valid_from, valid_to, prompt_version,
  captured_at}]->(:Entity)`
- `(:Entity)-[:REL {predicate, source_item, valid_from, valid_to, run_id}]->(:Entity)`

Query layer (`kb/query/hybrid.py`): `_detect_schema` probes `MATCH (si:SourceItem)`
→ `"s4"`; `_entities_of_items(item_ids)` walks `SourceItem-[MENTIONED]->Entity`;
`_point_hits(item_ids)` scrolls Qdrant on `item_id` and reads
`content_snippet`/`full_content`/`source_url`/`captured_at`/`tags`.

digital-twins today (`digital_twins/ingest/pipeline.py::_upsert_graph`):

```
MERGE (n:KbItem {source_url}) SET n.source, n.item_key, n.ts
MERGE (c:KbChunk {id}) SET c.text
MERGE (KbItem)-[:HAS_CHUNK]->(KbChunk)
```

Qdrant payload: `source, source_url, item_key, chunk_index, ts, text`
(+ `owner`/`owner_tag`).

## Field mapping (digital-twins -> S4 target)

| S4 / personal-kb field | digital-twins today | source of value |
|---|---|---|
| `SourceItem.item_id` | `item_key` (via `url = prefix + item_key`) | `item.key` — item identity, no change |
| `SourceItem.channel` | `source` (e.g. `fs`, `hermes`) | source name |
| `SourceItem.content_hash` | (inside point ID only) | NEW: `sha256(item.content)` item-level |
| Qdrant `item_id` | ❌ | = `item.key` (= `SourceItem.item_id`, same join key) |
| Qdrant `content_hash` | ❌ (per-chunk in ID) | item-level hash, shared by chunks |
| Qdrant `full_content` | `text` | rename |
| Qdrant `content_snippet` | ❌ | `text[:200]` |
| Qdrant `captured_at` | `ts` | rename |
| Qdrant `total_chunks` | ❌ | chunk count per item |
| Qdrant `source_type` | ❌ | `source.capability.source_type` or `source` |
| Qdrant `embed_model` | ❌ | config `embeddings.model` (BR-5.5 marker) |
| Qdrant `source_title` | ❌ | optional; default `""` |
| Qdrant `tags` | ❌ | `[]` (channel extras may supply) |
| Qdrant `owner`/`owner_tag` | top-level | move under `meta` |
| Qdrant `run_id` / `trigger` | ❌ (optional) | NOT part of the S4 schema — personal-kb's query layer never filters on them; they are BR-4.6 operational provenance. **Decision:** include them as *optional* passthroughs only when the pipeline already has them (it does: `run_id` L163, `trigger` L134). Values must equal the audit-row `run_id`/`trigger` (reuse the pipeline's existing arguments verbatim — no new value vocabulary; the four values in use are `manual` (CLI `--once`), `schedule` (scheduler loop), `mcp` (MCP dispatch), `web` (web app)). Not a contract field: a missing value must never break interop. |

## Point ID migration
`ids.py` today: `uuid5(NAMESPACE_URL, "prefix|item_key|idx|content_hash")`
(content-dependent). Target: `uuid5(NAMESPACE_DNS, "kb:{channel}:{item_id}:{idx}")`
(content-independent, matches personal-kb `kb/core/ids.py` so both systems share
one uuid5 ID space). Consequence: re-ingesting changed content
upserts the same point (BR-6.1 parity); `content_hash` becomes the staleness
signal instead of part of the ID.

**Join key (explicit):** the Qdrant payload `item_id` field and the Neo4j
`SourceItem.item_id` node property are **the same value** — the source's
`item.key`. personal-kb's `_point_hits` scrolls Qdrant on payload `item_id`,
and `_entities_of_items` matches `si.item_id IN $ids`; both resolve to the
same `item.key`. This is the single join key between the vector and graph
stores; it is NOT the `source_url` (which today keys `KbItem` but is dropped
in S4).

**Cross-system collision (recorded decision):** two systems writing to the
same Qdrant collection each mint point IDs from their own `channel` +
`item_id` pair. digital-twins source names (`fs`, `hermes`, `session`, …)
may overlap in *intent* with personal-kb channel registry values (`files`,
`sessions`, …) — e.g. digital-twins `session` vs personal-kb `sessions`,
`fs` vs `files`. If both systems ingest the *same underlying item* under
different channel/item_id values, they mint **different** point IDs for the
same content, yielding two points where NFR-1 expects one. **Decision:**
NFR-1 dedup is scoped to *within-system* (same trigger surface, same
`item.key`); cross-system content dedup is out of scope for this change. A
channel-mapping table (digital-twins source name → personal-kb channel)
would be required for cross-system dedup; add it only if personal-kb
introduces a channel-registry ACL that makes the mapping authoritative.
Until then, the two systems coexist in the same collection without
collision *by construction* (different `channel` strings) but without
cross-system dedup.

## Graph migration
- `:KbItem`/`:KbChunk`/`:HAS_CHUNK` are dropped; new code writes `:SourceItem` only.
- Existing nodes: one-shot cleanup task `MATCH (n:KbItem) DETACH DELETE n` +
  `MATCH (c:KbChunk) DETACH DELETE c`, exposed as a `digital-twins migrate s4`
  CLI step (idempotent, no-op on fresh DBs).
- No data is lost: graph content was redundant with Qdrant; Qdrant points are
  re-ingested (see Non-goals).
- **Chunk text leaves the graph:** today's `KbChunk` stores `c.text` in
  Neo4j; after S4 alignment, `:SourceItem` carries no chunk text — the chunk
  text lives in Qdrant's `full_content` payload field. Any Neo4j-only query
  consumer that reads chunk text from the graph will lose access; the
  vector store is the source of truth for chunk content. This is intended
  (Neo4j = entity/relation graph; Qdrant = vector + payload store).

## Recorded decisions
1. `channel` value set: keep digital-twins' own source names (`fs`, `hermes`,
   `pi`, `dsh`, `paperclip`, `imap_mail`, `custom`, `session`, …) as `channel`.
   S4 treats `channel` as opaque provenance; personal-kb's `_detect_schema`
   probes only `MATCH (si:SourceItem)` and never on `channel`, so no registry
   join is required for interop. **Revisit trigger:** if personal-kb adds a
   channel-registry ACL, the mapping must be reconciled (see cross-system
   collision decision above).
2. `content_hash` scope: item-level (shared by all chunks) per personal-kb
   BR-6.2, vs chunk-level (current). Decision: item-level — required for
   NFR-9 skip-if-unchanged and future supersede detection. Note: the two
   hashes have different scopes — the legacy `content_hash()` in `ids.py`
   hashes *chunk* text; the new `SourceItem.content_hash` and Qdrant
   payload `content_hash` hash *item* text. The deprecation entry (task 1.2)
   must make this distinction explicit.

## Open questions
1. **Cross-system dedup:** NFR-1 is scoped to within-system only (see
   cross-system collision decision). Should a channel-mapping table be
   introduced later to dedup the same underlying item across the two
   systems? Blocked on personal-kb channel-registry ACL.
2. **Neo4j chunk-text:** the S4 `SourceItem` node carries only
   `{item_id, channel, content_hash}`; chunk text lives in Qdrant
   `full_content`, not in Neo4j. Confirm no in-repo consumer expects
   chunk text in the graph (only tests do, and they will be updated by
   task 5.2).
