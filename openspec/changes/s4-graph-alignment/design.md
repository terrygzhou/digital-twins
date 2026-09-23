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
| `SourceItem.item_id` | `item_key` (via `url = prefix + item_key`) | item identity, no change |
| `SourceItem.channel` | `source` (e.g. `fs`, `hermes`) | source name |
| `SourceItem.content_hash` | (inside point ID only) | NEW: `sha256(item.content)` item-level |
| Qdrant `item_id` | ❌ | = `SourceItem.item_id` |
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

## Point ID migration
`ids.py` today: `uuid5(NAMESPACE_URL, "prefix|item_key|idx|content_hash")`
(content-dependent). Target: `uuid5(NAMESPACE_DNS, "kb:{channel}:{item_id}:{idx}")`
(content-independent, matches personal-kb `kb/core/ids.py` so both systems share
one ID space without collision). Consequence: re-ingesting changed content
upserts the same point (BR-6.1 parity); `content_hash` becomes the staleness
signal instead of part of the ID.

## Graph migration
- `:KbItem`/`:KbChunk`/`:HAS_CHUNK` are dropped; new code writes `:SourceItem` only.
- Existing nodes: one-shot cleanup task `MATCH (n:KbItem) DETACH DELETE n` +
  `MATCH (c:KbChunk) DETACH DELETE c`, exposed as a `digital-twins migrate s4`
  CLI step (idempotent, no-op on fresh DBs).
- No data is lost: graph content was redundant with Qdrant; Qdrant points are
  re-ingested (see Non-goals).

## open questions
1. `channel` value set: digital-twins source names (`fs`, `hermes`, `pi`,
   `dsh`, `paperclip`, `imap_mail`, `custom`, `session`) vs personal-kb channel
   registry (`files`, `sessions`, ...). Decision: keep digital-twins' own names
   as `channel` — S4 treats `channel` as opaque provenance; no registry join is
   required for interop. Revisit if personal-kb adds a channel-registry ACL.
2. `content_hash` scope: item-level (shared by all chunks) per personal-kb
   BR-6.2, vs chunk-level (current). Decision: item-level — required for
   NFR-9 skip-if-unchanged and future supersede detection.
