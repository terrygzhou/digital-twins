# Quickstart / Validation Guide: Web App (006)

The install → init → serve → open-browser-on-a-second-machine → sign in →
query → trigger-ingestion path for the 006 web app, exactly as shipped.
Host-neutral throughout (NFR-13 / FR-016): placeholders only — `<HOST>`,
`<STATE_DIR>`, `<A DIR OF .md FILES>`, `<EMAIL>`, `<PASSWORD>` — no literal
host path, username, install location, or interpreter pin.

> **Surface note (R8 / C-1):** 006 ships the chat **surface only**.
> `POST /api/kb/chat` is auth-checked, owner-scoped, and audit-worthy, but
> with no LLM endpoint configured it returns a clean `501
> not_implemented` with a remediation hint — full RAG answer generation is
> a follow-up slice. Everything else (sign-in, count, search, trigger,
> audit) is fully functional in this slice.

---

## 0. Conventions (host-neutrality, NFR-13 / FR-016)

- No host paths, usernames, install locations, or interpreter pins.
  Placeholders only: `<HOST>`, `<STATE_DIR>`, `<A DIR OF .md FILES>`,
  `<EMAIL>`, `<PASSWORD>`.
- **State directory:** the `KB_STATE_DIR` env var (the `state_dir` knob).
  All checks use `$KB_STATE_DIR` — never a literal path.
- **CLI:** `digital-twins` (the console script installed by `pip install .`).
- **Credentials are env vars / config, never argv:** `KB_*` knobs;
  the web UI itself authenticates via email + password + session token.
- The T021 portability guard scans this file once it is in its `SHIPPED`
  scope — keep it clean.

---

## 1. Prerequisites

**Server host** (the machine that will run the web app):

- A Python interpreter capable of building/installing the package (the
  package does not pin a minor version — no `python3.N` in this doc).
- Reachable endpoints, available as config knobs: `qdrant.url` (+ optional
  `qdrant.api_key`), and optionally `llm.endpoint` / `llm.model` /
  `llm.api_key` — the last three **only if you want chat**; without them the
  chat surface degrades to `501` by design (R8).
- A writable `KB_STATE_DIR` for the SQLite state store.
- A free TCP port for the web UI (default `8767` — distinct from the
  scheduler's `8765` and the MCP server's `8770`).

**Second machine** (the one with the browser): nothing but a browser. No
CLI, no package install (BR-11.1.8).

---

## 2. Install

```bash
# On the server host, in a clone of the source tree (hatchling build backend):
pip install .
# — or, once published to PyPI:
pip install digital-twins

# Confirm the console script is on PATH:
digital-twins --version
# expect: a version line, no host detail.
```

---

## 3. Configure (the `web.*` knobs + widening the bind)

The three web knobs (kept in lock-step with `docs/configuration.md` by
`tests/unit/test_knob_docs.py`):

| knob | default | env var | notes |
|---|---|---|---|
| `web.bind` | `127.0.0.1` | `KB_WEB__BIND` | Address the web UI binds to. `127.0.0.1` keeps it reachable only from the server host itself. |
| `web.port` | `8767` | `KB_WEB__PORT` | TCP port for the web UI server. |
| `web.base_url` | `http://localhost:8767` | `KB_WEB__BASE_URL` | Public URL of the web UI (embed/redirect links). |

**Fresh install:** all sources ship **disabled** (BR-11.2.7). Enable at least
one before triggering ingestion in §8 (e.g. `sources.fs.enabled: true` in
`kb.local.yml`, with `sources.fs.extra.dir` pointing at a directory of
`.md` files).

**Widening the bind for a second machine (R5):** by default the server binds
to `127.0.0.1` (BR-8.8 loopback-only), so only the server host can reach it.
To open it up to a second machine, set `web.bind=0.0.0.0` — either in
`kb.local.yml`:

```yaml
web:
  bind: 0.0.0.0
  port: 8767
```

or via env (env wins in the precedence chain):

```bash
export KB_STATE_DIR="<STATE_DIR>"
export KB_WEB__BIND=0.0.0.0
export KB_WEB__PORT=8767
# optional: export KB_WEB__BASE_URL=http://<HOST>:8767
```

> **Security note:** `0.0.0.0` makes the web UI reachable across the
> network. Sign-in is email + password with open sign-up on a fresh install
> (Q8) — the first account becomes `admin`, the rest `reader` (003 R7/C-4).
> Use this only where that trust model is acceptable; keep `127.0.0.1`
> otherwise.

---

## 4. Run the web server

```bash
digital-twins web
```

**Expect on stdout:**

```
web: listening on http://0.0.0.0:8767 (UI: http://0.0.0.0:8767/)
```

`digital-twins web` is its own subcommand: it does **not** start the
scheduler loop and does **not** write the scheduler pidfile (distinct from
`digital-twins serve`). Ctrl-C stops it cleanly.

---

## 5. Open the browser (on the second machine)

On the phone / laptop / remote host — no CLI, just a browser:

```
http://<HOST>:8767
```

`<HOST>` is the server host's reachable address (its LAN IP, hostname, or
tunnel endpoint — chosen by you; this doc does not pin one). The page
serves the single static UI (`GET /` → `index.html` + `/static/style.css`);
plain HTML, no build step, no SPA framework (R4). On the server host itself
the same page is at `http://127.0.0.1:8767` when the bind is not widened.

---

## 6. Sign in / sign up

The UI shows two cards before a session exists:

- **Sign up** (first-time users on a fresh install): the first account
  created is role `admin`; every later account is `reader` (003 R7/C-4,
  shared with CLI `init`/`signup`). No OAuth, no email verification, no
  admin-gating (Q8).
- **Sign in** (existing accounts): email + password.

Both cards drive the same `/api/auth/*` endpoints an external client hits:

| card | endpoint | success | failure |
|---|---|---|---|
| Sign up | `POST /api/auth/signup` | `200 {"email","role","created":true}` | `409` duplicate email · `400` blank field |
| Sign in | `POST /api/auth/signin` | `200 {"session_token","expires_at"}` | `401` unknown email / wrong password |
| Sign out | `POST /api/auth/signout` | `200 {"revoked":true}` | `401` no/invalid token |

After sign-in the UI stores the session token and sends it as
`Authorization: Bearer <token>` on every subsequent `/api/*` request (a
`?token=` query fallback is accepted). An absent, expired, or revoked token
is rejected with `401` on every non-auth endpoint (FR-017, fail-closed). The
**Sign out** button revokes the session server-side.

`curl` equivalent (if you prefer to verify the same surface by hand):

```bash
# first account on a fresh install → role: admin
curl -s -X POST http://<HOST>:8767/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"<EMAIL>","password":"<PASSWORD>"}'
# → {"email":"<EMAIL>","role":"admin","created":true}

curl -s -X POST http://<HOST>:8767/api/auth/signin \
  -H 'Content-Type: application/json' \
  -d '{"email":"<EMAIL>","password":"<PASSWORD>"}'
# → {"session_token":"...","expires_at":"..."}
```

Keep the `session_token`; pass it as `Authorization: Bearer <token>` (or
`?token=`) on every `/api/*` call.

---

## 7. Use the KB panel (count, search, trigger ingestion)

Once signed in, the KB panel is the main surface. It drives the same
`/api/*` endpoints an external client hits (SC-006 — no parallel
client-side logic).

### Point count

The panel shows the collection-wide point count, the caller's owner-scoped
count, and a small sample of recent rows:

```bash
curl -s 'http://<HOST>:8767/api/kb/points?limit=5' -H 'Authorization: Bearer <T>'
# → {"count": 120, "owner_count": 42, "sample": [
#     {"source":"fs","source_url":"...","chunk_index":0,"text":"..."} ]}
```

`GET /api/kb/points?source=&limit=` (default limit 5, cap 100) →
`{"count","owner_count","source_count"?:…,"sample":[…]}`. With an empty
collection the counts are 0 and the sample is empty — not an error. If
Qdrant is unreachable the panel surfaces a `502`/`503` with a remediation
hint, not a crash.

`GET /api/me` also backs the "who am I" read:
`{"email","role","point_count"}` for the caller's owner scope.

### Search

Type a query; the panel `POST`s it to the search endpoint:

```bash
curl -s -X POST http://<HOST>:8767/api/kb/search \
  -H 'Authorization: Bearer <T>' -H 'Content-Type: application/json' \
  -d '{"query":"<QUERY>","limit":5}'
# → {"results":[{"score":0.87,"source_url":"...","text":"...",
#                 "source":"fs","chunk_index":0}, ...]}
```

`POST /api/kb/search` with body `{"query","limit"?}` → top-N results (each
with `score`, `source_url`, `text`, `source`, `chunk_index`), sorted by
descending score, scoped to the caller's `owner_tag`. A blank query is
rejected with `400 {"error":"query must be a non-empty string"}`.

### Trigger ingestion

The panel's **Trigger ingestion** button (per enabled source, or run-all-
enabled) `POST`s to the run endpoint:

```bash
curl -s -X POST http://<HOST>:8767/api/ingest/run \
  -H 'Authorization: Bearer <T>' -H 'Content-Type: application/json' \
  -d '{"source":"fs"}'
# → run summary {"run_id","status","counts","points"}
```

`POST /api/ingest/run` with `{"source":"<name>"}` (or `{"source":"all"}` /
`{}`) runs `digital_twins.ingest.pipeline.run_pipeline` — the **same code
path** as `digital-twins run --once` and MCP `kb_ingest` (R3) — with
`trigger="web"` and `scheduled_by=<caller-email>`, points owner-stamped with
the caller's `owner_tag`.

| outcome | response |
|---|---|
| run succeeded | `200` run summary + a `trigger="web"` audit row |
| caller lacks `trigger_run` (reader) | `403 {"code":"permission_denied","message":"role 'reader' may not trigger a run (capability 'trigger_run')"}` — no audit row |
| source not enabled in config | `400 {"error":"source '<name>' is not enabled"}` |
| unknown source name | `400 {"error":"unknown source '<name>'"}` |
| no sources enabled | `400 {"error":"no sources enabled"}` |
| enabled source missing prerequisites | `409`/`500` naming the missing prerequisite(s); a `failed` audit row is written (fail-fast — never silently ingest zero) |

**One-record-not-N (NFR-1 / NFR-14, SC-002):** re-triggering the same source
for the same window does **not** duplicate points — the total point count is
unchanged (high-water + deterministic chunk IDs). The web UI and
`run --once` yield the **same points** for the same source + window; the
owner tag is a payload field only, never part of the point ID.

---

## 8. View the audit

The KB panel shows the **last audit row** for the caller, from
`GET /api/audit/recent?limit=` (default 10, cap 100):

```bash
curl -s 'http://<HOST>:8767/api/audit/recent?limit=10' -H 'Authorization: Bearer <T>'
# → {"rows":[{"run_id":"...","started_at":"...","completed_at":"...",
#             "status":"ok","trigger":"web","scheduled_by":"<EMAIL>",
#             "per_source_counts":{"fs":3}}, ...]}
```

- A non-admin sees only rows where `scheduled_by` equals their own email
  (NFR-16); an admin sees all rows.
- A web-triggered run from §7 appears here immediately with
  `trigger="web"` and `scheduled_by=<caller-email>`.

---

## 9. Chat (501 until an LLM endpoint is set)

The chat card is the 006 **surface** (C-1 / R8): `POST /api/kb/chat` with
`{"query":"<text>"}` is auth-checked, owner-scoped, and audit-worthy — but
full RAG answer generation is **out of scope** for this slice.

```bash
curl -s -X POST http://<HOST>:8767/api/kb/chat \
  -H 'Authorization: Bearer <T>' -H 'Content-Type: application/json' \
  -d '{"query":"<QUERY>"}'
# no llm.endpoint configured → 501:
# → {"code":"not_implemented","remediation":"set llm.endpoint / llm.model to enable chat (006 ships the surface only; R8)"}
```

- **No `llm.endpoint` / `llm.model` configured** → clean `501` (not a crash,
  not a `401` — the auth check runs first).
- **Blank query** → `400 {"error":"query must be a non-empty string"}`.
- **LLM endpoint configured** → the request is authenticated, owner-scoped,
  and audit-worthy; actual answer generation is a follow-up slice (the
  endpoint contract is stable, so generation lands without re-plumbing
  auth/audit/scoping).

---

## Status-code summary

| code | where | meaning |
|---|---|---|
| `200` | all success paths | auth ok, capability ok. |
| `400` | search/chat blank query; ingest source not enabled / unknown / none enabled | bad or missing input. |
| `401` | every non-auth `/api/*` endpoint | no/invalid/expired/revoked session token (fail-closed, FR-017). |
| `403` | `POST /api/ingest/run` as reader | caller lacks the `trigger_run` capability. |
| `409` | duplicate email on signup; ingest missing prerequisites | conflict / fail-fast. |
| `501` | `POST /api/kb/chat` with no LLM endpoint | chat surface, not_implemented (R8). |
| `502`/`503` | point-count / search | Qdrant unreachable, with a remediation hint. |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` on `/api/*` | no/invalid/expired/revoked session token — sign in again. |
| `403 permission_denied` on `/api/ingest/run` | caller role is `reader` (lacks `trigger_run`). |
| `400 source '<x>' is not enabled` | the source is disabled in config; enable it (fail-fast). |
| `409` on signup | email already exists. |
| `501` on chat | no `llm.endpoint` configured — expected in 006 (C-1/R8); set it to enable. |
| `502`/`503` on points/search | Qdrant unreachable — check `qdrant.url` / network. |

---

## Running the tests

```bash
pytest tests/unit tests/integration -q
# standing guards (must stay green): tests/integration/test_portability.py
# (T006/T021 — this file is in its SHIPPED scope once T021 has scanned it)
# and tests/unit/test_knob_docs.py (T027).
```

---

## Exit criteria

- **SC-001 (BR-11.1.8):** on a fresh install with `web.bind=0.0.0.0`, a
  browser on a *different* machine can sign up (first account → `admin`),
  sign in, and reach `GET /api/me` — all with **no CLI on that machine**.
- **SC-002 (NFR-1 / NFR-14):** the same content ingested via the web UI's
  "Trigger ingestion" yields the **same** points as
  `digital-twins run --once` for the same source + window (one record, not
  N); a `trigger="web"` audit row is written.
- **SC-003:** all three new knobs (`web.bind`, `web.port`, `web.base_url`)
  are present in `digital_twins/config/knobs.py`, `config.example.yml`,
  `.env.example`, and `docs/configuration.md`; `tests/unit/test_knob_docs.py`
  + `tests/integration/test_portability.py` stay green (constitution IV,
  NFR-13).
- **SC-004:** every `/api/*` endpoint except the three public auth endpoints
  is `401` without a valid session token; `trigger_run`-gated endpoints are
  `403` for reader role (003 R3).
- **SC-005:** a non-admin's `/api/audit/recent` lists only their own rows;
  an admin's lists all (NFR-16).
- **SC-006 (R4):** the static UI (plain HTML, no build step) renders
  sign-in, sign-up, and the KB panel (point count, search, trigger-ingest,
  last-audit-row) and drives them through `/api/*` only — no parallel
  client-side logic.
- **Chat (C-1 / R8):** the chat surface is `501 not_implemented` with a
  remediation hint naming `llm.endpoint` until an LLM endpoint is configured.
- **Host-neutral throughout (NFR-13 / FR-016):** no host path, username,
  install location, or interpreter pin — verified by the portability guard
  (T006/T021) once this file is in its scan scope.
