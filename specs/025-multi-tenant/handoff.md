# Handoff — 025 Multi-Tenant Data Isolation

**Date**: 2026-11-15
**Status**: Investigation complete (diagnostics done, no code written)
**Feature branch**: `025-multi-tenant`
**Status of spec**: Decided (T1–T4 resolved); `data-model.md` and `spec.md` authored. `tasks.md` **not yet generated** (plan stage not run).

## What was decided

See `spec.md` §Decisions (T1–T4) and `data-model.md`. Summary:

- **T1**: Per-tenant hard boundary — dedicated Neo4j database (`?db=<tenant>`) + dedicated Qdrant collection (`kb-<slug>`) per tenant. Not per-user. Label-prefix boundary (A4) rejected.
- **T2**: Copy-in-place backfill (`qdrant_client.upload_points`), no re-embedding. Neo4j tenant DBs created empty; legacy graph copied cross-DB via one-shot migration.
- **T3**: Install-level admin role; legacy `admin` migrates to it.
- **T4**: No per-tenant endpoint overrides in v1 (NFR-13 install-global).
- **Superseded**: A4 (label prefix), A5 resolved → MAJOR version bump.

## Live-environment findings (2026-11-15)

### Container topology (verified by `docker ps` + `docker inspect`)

| Role | Container | Ports | Auth | Volume |
|---|---|---|---|---|
| shared/local Qdrant | `qdrant` (local image `b3063c673f39`, qdrant v1.9.7) | `127.0.0.1:6333→6333`, `6334→6334` | **none** (loopback-open) | container-local, no named volume |
| keyva-dedicated Qdrant | `qdrant-dt` (`qdrant/qdrant:v1.9.7`) | `127.0.0.1:6335→6333`, `6336→6334` | API-key gated (`QDRANT__SERVICE__API_KEY`) | named volume `dt-local-deploy_qdrant-dt-data` |
| shared/local Neo4j | `neo4j` (`neo4j:5`) | `127.0.0.1:7474→7474`, `7687→7687` | `neo4j/graphrag_terry_2026` | container-local |
| keyva-dedicated Neo4j | `neo4j-dt` (`neo4j:5`) | `127.0.0.1:7475→7474`, `7689→7687` | `neo4j/dt-neo4j` | container-local |

Both Qdrant instances run the **same engine** (v1.9.7). Both Neo4j instances run `neo4j:5`.

### Qdrant collection inventory

| Instance | Collection | Points | Notable payload-index fields |
|---|---|---|---|
| 6333 | `personal_kb` | 1,506,108 | `source` |
| 6333 | `keyva_kb` | 29,991 | — |
| 6333 | `Vector_index_312bd383_3759_496a_a801_641414386c1b_Node`, `digital-and-marketing`, `ev_group`, `leadership`, `mem0`, `mem0_terry`, `mem0migrations`, `star_charts` | — | (other tenants/tools) |
| 6335 | `digital-twins-kb` | 2,298,000 | — |
| 6335 | `personal_kb` | 4,501,567 | `item_key`, `embed_model`, `reembedded_at`, `category` |

### Date-field schema (cross-tenant)

All Qdrant `captured_at` / `ts` / `reembedded_at` are **ISO-8601 UTC strings** (Qdrant has no temporal type). The only Neo4j temporal-type outlier is on the **shared/local** `neo4j` container, not in keyva.

| Store | 6333 `personal_kb` | 6333 `keyva_kb` | 6335 `digital-twins-kb` | 6335 `personal_kb` |
|---|---|---|---|---|
| `captured_at` | `…T16:12:08.805086+00:00` (µs) | `…T04:40:45.409229+00:00` (µs) | `…T06:38:48.000000+00:00` (date-prec) | `…T14:46:42.000000+00:00` |
| `ts` | **absent** | present (dup of `captured_at`) | present | present |
| `reembedded_at` | — | — | — | `2026-11-11T05:47:07Z` (133,704 pts) |
| `item_key` | — | present | present | present |

| Neo4j instance | `:SourceItem` keys | `:Entity` keys | `:Event` | rel date props |
|---|---|---|---|---|
| `neo4j` (shared/local) | `item_id, channel, content_hash, last_run_id, last_captured_at` (string) | `name, type, created, desc, last_seen` (string) | `name, last_seen` (**`DateTime` temporal**) | `MENTIONED`/`REL` = `captured_at, valid_from, valid_to, prompt_version` (string); `RELATED_TO`/`AT`/`CONTAINS` `last_seen` = **`DateTime`** |
| `neo4j-dt` (keyva) | `item_id, channel, content_hash, last_run_id, last_captured_at` (string) | `name, type, created, desc, last_seen` (string) | **absent** | `MENTIONED`/`REL` = `captured_at, valid_from, valid_to, prompt_version, catalog_version` (string) |

### Cross-tenant observations for 025

1. **`ts` vs `captured_at` duplication** — `keyva_kb`, `digital-twins-kb`, and 6335 `personal_kb` all carry **both** `ts` and `captured_at` (identical value). 6333 `personal_kb` (written by the older hermes-skill `capture.py`) has only `captured_at`. The `ts` field is a legacy vestige from the hermes-skill source. 025 should keep `captured_at` as the canonical field and drop `ts` during backfill (or tolerate it as inert).
2. **`reembedded_at` only on 6335 `personal_kb`** (133,704 pts, 2026-11-11) — the re-embed sweep ran against the 6335 keyva-dedicated tenant only. The 6333 `personal_kb` is **not** reembedded. 025 backfill (T2) must preserve `reembedded_at` where present or record that the 6333 copy is the stale one.
3. **`item_key` indexing only on 6335 `personal_kb`** — the 6335 tenant was set up for the keyva SOP's large-scale workflow; 6333 `personal_kb` indexes only `source`. 025's `kb-<slug>` collections should index `item_key` (and `embed_model`) by default.
4. **Keyva tenant spans both Qdrant instances** — the tenancy-options doc says "keyva = `qdrant-dt` :6335 + collection `digital-twins-kb`", which is true for the **backfill/seed** corpus (2.3M pts, date-precision, `digital-twins-kb`). But the **live dt-mcp incremental pipeline** writes `keyva_kb` (29,991 pts, microsecond dates) into the shared 6333 instance. So keyva's working data is split: seed on 6335, live ingest on 6333. 025 must decide whether 6333 `keyva_kb` migrates into the 6335 tenant or is a separate tenant.
5. **6335 `personal_kb` is 3× the 6333 `personal_kb`** (4.5M vs 1.5M) and is the post-reembed copy. 025's T2 backfill should use the 6335 copy as the source of truth for the personal-kb tenant and treat 6333 `personal_kb` as the legacy hermes-skill artifact.
6. **Neo4j `DateTime` temporal outlier is local-only** — `neo4j` container's `:Event.last_seen` and `RELATED_TO`/`AT`/`CONTAINS` `last_seen` are Neo4j `DateTime` types; everything on `neo4j-dt` (keyva) and all node-property dates is ISO-8601 string. 025's copy-in-place migration should normalize the local `DateTime` columns to UTC ISO strings when copying into tenant DBs (or accept mixed types and document it).
7. **Auth asymmetry** — 6333 is open on loopback; 6335 is API-key gated. 025's `tenant_service_tokens` table (migration v6) is the package-side analogue; the container-side key is ops config, not package config.
8. **`digital-twins-kb` has no payload index** — 2.3M pts with zero indexed payload fields. 025 should add `item_key` / `embed_model` indexes as part of tenant bootstrap.

## What's pending

- [ ] `tasks.md` generation (spec-kit plan stage) — spec is decided, tasks not yet generated
- [ ] Decision: does 6333 `keyva_kb` (live ingest) become part of the 6335 keyva tenant, or is it a separate tenant?
- [ ] Decision: source of truth for personal-kb tenant backfill — 6335 `personal_kb` (4.5M, post-reembed) or 6333 `personal_kb` (1.5M, pre-reembed)?
- [ ] Decision: normalize `:Event.last_seen` and rel `last_seen` `DateTime` → UTC string during copy-in-place, or accept mixed types?
- [ ] Add `item_key` / `embed_model` payload indexes to 025 tenant-bootstrap defaults
- [ ] Drop `ts` duplicate field during backfill (or document as inert)

## Files touched this session

- `specs/025-multi-tenant/spec.md` — read (T1–T4 decisions, two-phase plan, superseded A4/A5)
- `specs/025-multi-tenant/data-model.md` — read (migration v6 DDL, Qdrant/Neo4j store mapping)
- `specs/025-multi-tenant/handoff.md` — **this file** (new)

## Diagnostics run (reproducible)

```bash
# Container topology
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker inspect qdrant-dt --format '{{range .Config.Env}}{{println .}}{{end}}'
docker inspect qdrant  --format '{{.Mounts}}'

# Qdrant 6333 (open, no auth)
curl -sS http://localhost:6333/collections
curl -sS http://localhost:6333/collections/personal_kb
curl -sS -X POST http://localhost:6333/collections/personal_kb/points/scroll \
     -H 'Content-Type: application/json' -d '{"limit":5,"with_payload":true}'

# Qdrant 6335 (API-key gated)
K='api-key: 1bc6c4b1305b06c2b22ef0f7f95a97bc1215407a419a61fbf50b0718c73c069e'
curl -sS -H "$K" http://localhost:6335/collections
curl -sS -H "$K" -X POST http://localhost:6335/collections/digital-twins-kb/points/scroll \
     -H 'Content-Type: application/json' -d '{"limit":3,"with_payload":true}'

# Neo4j local (shared)
docker exec neo4j cypher-shell -u neo4j -p graphrag_terry_2026 \
     'MATCH (n:SourceItem) RETURN keys(n) AS keys, count(*) AS c LIMIT 8'
docker exec neo4j cypher-shell -u neo4j -p graphrag_terry_2026 \
     'MATCH (n:Event) WHERE n.last_seen IS NOT NULL RETURN n.last_seen, toString(n.last_seen)=n.last_seen AS is_str LIMIT 2'
# is_str = FALSE → DateTime temporal type

# Neo4j-dt (keyva)
docker exec neo4j-dt cypher-shell -u neo4j -p dt-neo4j \
     'MATCH (n:SourceItem) WHERE n.last_captured_at IS NOT NULL RETURN n.last_captured_at, toString(n.last_captured_at)=n.last_captured_at AS is_str LIMIT 2'
# is_str = TRUE → ISO-8601 string
```

## Next agent's starting point

1. **Read** `spec.md` §Decisions + `data-model.md` (already authored).
2. **Generate `tasks.md`** via spec-kit plan stage (`.pi/prompts/speckit.plan`).
3. **Resolve the three open decisions** in "What's pending" — each is a one-line ADR entry in `spec.md`.
4. **Implement** per `tasks.md`; the migration is additive-only (Constitution VI / NFR-15), `user_version` 5→6, transactional.
5. **Guard notes** (one line each, per spec.md): `neo4j_migration.py` + `graph_query.py` — "pin the URL explicitly when operating a tenant DB".
