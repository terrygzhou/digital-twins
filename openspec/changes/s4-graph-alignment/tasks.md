# Tasks: s4-graph-alignment

## 1. Point-ID + payload schema (digital_twins/ingest/ids.py)
- [ ] 1.1 Add content-independent `point_id(channel, item_id, chunk_index)`
      using `uuid5(NAMESPACE_DNS, f"kb:{channel}:{item_id}:{chunk_index}")`;
      keep `content_hash()` and add item-level usage. Test-First: unit tests in
      `tests/unit/test_ids.py` (idempotence, content-independence, no
      cross-system collision with personal-kb's namespace string form).
- [ ] 1.2 Deprecate the old content-dependent `point_id(prefix, item_key,
      chunk_index, text)` (keep for one release, mark deprecation in
      `digital_twins/config/deprecation.py`).

## 2. Qdrant payload alignment (digital_twins/ingest/pipeline.py)
- [ ] 2.1 Extend payload dict to S4/personal-kb fields: `item_id`,
      `content_hash` (item-level, shared by chunks), `full_content`,
      `content_snippet` (text[:200]), `captured_at` (rename of `ts`),
      `total_chunks`, `embed_model` (from config), `source_type`, `tags: []`;
      move `owner`/`owner_tag` under `meta`.
- [ ] 2.2 Unit tests: payload contract assertions (field presence, item-level
      hash shared across chunks of one item, owner under meta).

## 3. Neo4j S4 graph (digital_twins/ingest/pipeline.py::_upsert_graph)
- [ ] 3.1 Replace `_upsert_graph` with `MERGE (si:SourceItem {item_id: $id})
      SET si.channel = $ch, si.content_hash = $hash` per item (deduped);
      drop `KbItem`/`KbChunk`/`HAS_CHUNK` writes.
- [ ] 3.2 Update `digital_twins/health.py` / any graph health check to probe
      `:SourceItem` instead of `:KbItem`.

## 4. Migration + CLI
- [ ] 4.1 Add `digital-twins migrate s4` step: `MATCH (n:KbItem) DETACH
      DELETE n` + `MATCH (c:KbChunk) DETACH DELETE c` (idempotent, no-op on
      fresh DBs), plus a `--dry-run` reporting node counts.
- [ ] 4.2 Integration test: fresh-DB + migration-DB paths (Neo4j test
      container), asserting old labels gone, `:SourceItem` present.

## 5. Spec-013 test-contract delta (REQUIRED by AGENTS.md / SDD)
- [ ] 5.1 Update `specs/013-neo4j-query-tests/plan.md` pinned Cypher from the
      KbItem/KbChunk shape to the S4 `:SourceItem` shape.
- [ ] 5.2 Update corresponding tests in `tests/integration/` (neo4j query
      contract cases) and `.superpowers/sdd/013-neo4j-query-tests/progress.md`
      ledger.

## 6. Docs + config
- [ ] 6.1 `config.example.yml` + docs: document new payload fields and the
      `embed_model` requirement; note re-ingest requirement after upgrade
      (old points orphaned by ID-scheme change).
- [ ] 6.2 Portability guard check: `pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py` must stay green (no host paths introduced).

## 7. Verification
- [ ] 7.1 Full pytest run; NFR-1 acceptance check (same content via schedule /
      run / mcp / ui = one point) re-asserted against new point-ID scheme.
- [ ] 7.2 Manual interop check: point personal-kb's `kb_health`/hybrid query
      at a digital-twins-populated DB and confirm `_point_hits` finds
      digital-twins items by `item_id`.
