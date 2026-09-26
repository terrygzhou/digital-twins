# UAT Report — digital-twins 0.11.2 (UI / BR-11 + NFR-12..17)

Run: 2026-07-19 · Local install (`pip install .`, Python 3.11) · Web UI on `127.0.0.1:8767`
State dir: `/tmp/uat-state` (fresh) · Qdrant 6333 (384-dim, 643612 pts under `terry.zhou@ymail.com-ingest`), Neo4j 7687, LLM/embedding on 8000/8080 (sglang, non-embedding)
Note: UAT used a hash-based embedding stand-in on `127.0.0.1:8091` (`KB_EMBEDDING__ENDPOINT=http://127.0.0.1:8091`, no `/v1` suffix) because configured `embedding.endpoint` points at a non-embedding model.

## Results

| # | Case (UI) | Requirement | Result | Evidence |
|---|-----------|-------------|--------|----------|
| 1 | UI loads (index + css, 35088 b) | BR-11.1 | PASS | `GET /` 200 |
| 2 | Unauthed API access | NFR-17/5 | PASS | `/api/auth/me` 401 before sign-in |
| 3 | Sign up first account → admin | BR-11.4 | PASS | `terry.zhou@ymail.com` created via UI flow; role admin |
| 4 | Sign in + me (point_count 643612) | BR-11.1 | PASS | `/api/auth/me` |
| 5 | Duplicate sign up | BR-11.4 | PASS | 409 |
| 6 | Bad credentials | — | PASS | 401 |
| 7 | `/api/kb/points` | BR-11 | PASS | count 952189 (global), 643612 owner-scoped |
| 8 | Search (admin: 3 hits; reader: 0) | NFR-16/17 owner-scoping | PASS | owner-scoped |
| 9 | Blank search / chat | — | PASS | 400 |
| 10 | Chat | — | PASS (surface) | 501 not_implemented hint shown |
| 11 | Audit recent rows | NFR-16 | PASS | rows returned; reader sees own rows only |
| 12 | Services panel GET (admin) | FR-004, NFR-18 | PASS | `*_set` booleans only, no raw creds; `env_overrides` shown |
| 13 | Services POST unknown service | — | PASS | 404 |
| 14 | Channels GET/POST (admin) | BR-11 | PASS | persists to `kb.local.yml`; unknown source 404 |
| 15 | Reader gating: ingest 403, config 403, search allowed, audit own-rows | BR-11.4.4, NFR-16/17 | PASS | role-scoped |
| 16 | **Trigger ingest (admin)** | BR-11.2, NFR-1/12 | **BLOCKED — BUG-01** | see below |

## Blocker: BUG-01 — graph write crashes every ingest

`POST /api/ingest/run` → `500 "ingest run failed: see server log for details"`.
Root cause: `pipeline.py:335` (`_ensure_graph_schema`) and `:317` (`_upsert_graph`) call
`neo4j.run(...)`, but `scheduler/loop.py:258` `build_neo4j_driver` returns a raw
`neo4j.GraphDatabase.driver` (BoltDriver) which has no `.run()`.
Unit tests fake a driver-shaped `.run()` so the suite stays green.

Repro (admin token):
```
curl -sS -X POST -H "Authorization: Bearer $(cat /tmp/uat-token.txt)" \
  -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8767/api/ingest/run
# → 500 ingest run failed: see server log for details
# traceback: AttributeError: 'BoltDriver' object has no attribute 'run'
#   at digital_twins/ingest/pipeline.py:335
```
Secondary finding: the catch-all at `web/app.py:973-976` swallows the traceback;
server log shows only WARNINGs — observability gap.

Without neo4j creds: preflight (`health.py:323`) treats neo4j as a hard dep →
fail-fast "service neo4j is unconfigured" even in Qdrant-only mode. So ingestion
cannot complete at all in the current code; NFR-1 dedup (point count stable
across reruns) could not be verified end-to-end. Point count stayed 643612
across failed reruns (no corruption, but no successful write either).

## Config side-effect
UAT channel POSTs persisted `sources.fs.max_items: 50`, `sources.hermes.max_items: 10`
into `~/.config/digital-twins/kb.local.yml` (real file). Restore if desired.

## Still running
- Web server pid `/tmp/uat-web.pid` on 8767
- Embed stand-in pid `/tmp/uat-embed.pid` on 8091

## NFR mapping (BR-11 / NFR-12..17 scope)

- **NFR-12 (portability)** — install+init path exercised via `pip install .`; first-ingestion-in-<30-min
  criterion **NOT met** in this run due to BUG-01 + neo4j preflight hard-dep.
- **NFR-13 (environment neutrality)** — no host path/username pin surfaced in the UI surface or
  config defaults inspected; PASS on observed evidence.
- **NFR-14 (idempotent scheduling)** — **BLOCKED** by BUG-01; no successful write to dedup-verify.
- **NFR-15 (upgrade safety)** — out of UAT scope (no in-place upgrade performed this run).
- **NFR-16 (per-user auditability)** — PASS: reader token lists only own audit rows; admin lists all.
- **NFR-17 (credential scoping)** — PASS: reader cannot trigger ingest (403) or read config
  services/channels (403); search is owner-scoped (reader gets 0 hits vs admin's 3).

## Out-of-scope (declined by user)
- **BUG-01 fix** (`_upsert_graph`/`_ensure_graph_schema` vs BoltDriver) — not implemented this session.
- **Swallowed ingest traceback** (`web/app.py:973-976` catch-all) — not implemented this session.
