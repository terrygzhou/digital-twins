# Quickstart: 006 Web App

**Feature**: `specs/006-web-app` · **Goal**: sign in to the KB from a browser on
another machine and query / trigger ingestion — no CLI on that machine.

> All paths below use `KB_STATE_DIR` (your own state dir) and the config layer.
> No host path, username, or install location is assumed (NFR-13).

## Prerequisites

- `digital-twins` installed (v0.5.0+): `pip install digital-twins` (or
  `pip install -e .` from the repo).
- Your own endpoints configured (a `kb.local.yml` / `.env` / env vars):
  `qdrant.url`, `qdrant.api_key` (optional), and `llm.endpoint` / `llm.model`
  / `llm.api_key` **only if you want chat** (otherwise chat returns `501`,
  by design in 006 — C-1/R8).
- A writable `KB_STATE_DIR` for the SQLite state store.

## 1. Configure the web surface

Pick the bind + port (defaults: `web.bind=127.0.0.1`, `web.port=8767`):

```bash
export KB_STATE_DIR="$HOME/state"      # your own state dir
export KB_WEB__BIND=0.0.0.0            # widen to reach across the network
export KB_WEB__PORT=8767               # free port (distinct from 8765 / 8770)
# optional: export KB_WEB__BASE_URL=http://<host>:8767
```

(Or put `web.bind` / `web.port` / `web.base_url` in `kb.local.yml`.)

## 2. Start the web app

```bash
digital-twins web
# → web: listening on http://0.0.0.0:8767 (UI: http://<host>:8767/)
```

(`digital-twins web` is a separate surface from `digital-twins serve` (the
scheduler) — it does not start the scheduler loop; it only serves the UI + API.)

## 3. Sign up / sign in (open sign-up on a fresh install, Q8)

From a browser on a **different machine** (or `curl`):

```bash
# first account on a fresh install → role: admin
curl -s -X POST http://<host>:8767/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"hunter2-secret"}'
# → {"email":"you@example.com","role":"admin","created":true}

curl -s -X POST http://<host>:8767/api/auth/signin \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"hunter2-secret"}'
# → {"session_token":"...","expires_at":"2026-..."}
```

Keep the `session_token`; pass it as `Authorization: Bearer <token>` (or
`?token=`) on every `/api/*` call.

## 4. Use the KB

```bash
T=<session_token>
H='Authorization: Bearer <T>'

# who am I + how many points in my scope?
curl -s http://<host>:8767/api/me -H "$H"
# → {"email":"you@example.com","role":"admin","point_count":42}

# point count + sample
curl -s 'http://<host>:8767/api/kb/points?limit=5' -H "$H"

# search
curl -s -X POST http://<host>:8767/api/kb/search \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{"query":"how do I deploy with SGLang","limit":5}'

# trigger ingestion (admin/scheduler only; reader → 403)
curl -s -X POST http://<host>:8767/api/ingest/run \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{"source":"fs"}'
# → run summary {run_id,status,counts,points}  (audit row: trigger="web")

# recent audit (non-admin: only your rows; admin: all)
curl -s 'http://<host>:8767/api/audit/recent?limit=10' -H "$H"

# chat (surface only in 006; 501 until an LLM endpoint is configured)
curl -s -X POST http://<host>:8767/api/kb/chat \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{"query":"summarize my recent notes"}'
```

Open the UI at `http://<host>:8767/` — the sign-in, sign-up, and KB panel
(point count, search, Trigger ingestion, last-audit-row) all drive the **same**
`/api/*` surface shown above (SC-006).

## 5. Sign out

```bash
curl -s -X POST http://<host>:8767/api/auth/signout -H "$H"
# → {"revoked":true}
```

## Dedup parity (the top acceptance check)

The same content ingested via `digital-twins run --once` and via the web UI's
Trigger ingestion yields **one** point, not two (NFR-1, NFR-14) — the web path
goes through `digital_twins.ingest.pipeline` (R3), and the owner tag is a
payload field only, never part of the point ID.

## Running the tests

```bash
pytest tests/unit tests/integration -q
# standing guards (must stay green): tests/integration/test_portability.py,
# tests/unit/test_knob_docs.py
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` on `/api/*` | no/invalid/expired/revoked session token — sign in again. |
| `403 permission_denied` on `/api/ingest/run` | caller role is `reader` (lacks `trigger_run`). |
| `400 source '<x>' is not enabled` | the source is disabled in config; enable it (fail-fast). |
| `409` on signup | email already exists. |
| `501` on chat | no `llm.endpoint` configured — expected in 006 (C-1/R8); set it to enable. |
| `502`/`503` on points/search | Qdrant unreachable — check `qdrant.url` / network. |
