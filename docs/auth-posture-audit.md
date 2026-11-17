# digital-twins Local Auth Posture Audit

> **2026-06-16** — Security posture review of the `digital-twins` 0.11.3
> local deployment (`~/.sops/dt-local-deploy/`), covering the four
> long-lived services: `qdrant-dt`, `neo4j-dt`, `digital-twins` (MCP),
> and the `personal-kb` `qdrant` + `neo4j` pair.
>
> Scope: what credentials exist, which endpoints enforce them, what
> would be needed to close gaps, and the residual trust surface of the
> stdio MCP channel.

---

## 1. Service inventory & auth posture

| Service | Port(s) | Credential | Auth mechanism | Enforced? |
|---|---|---|---|---|
| **qdrant-dt** (dt deploy) | `127.0.0.1:6335` REST, `127.0.0.1:6336` gRPC | *none* | — | **No** |
| **neo4j-dt** (dt deploy) | `127.0.0.1:7475` HTTP, `127.0.0.1:7689` bolt | `neo4j` / `dt-neo4j` | `NEO4J_AUTH=neo4j/dt-neo4j` env → Basic auth on every HTTP & bolt session | **Yes** |
| **digital-tokens** (MCP) | `127.0.0.1:8770` | `Bearer <DT_SERVICE_TOKEN>` *or* personal token *or* session token | `mcp_authenticator` (service → personal → session), constant-time compare | **Yes** (401 / 403 on failure) |
| **personal-kb qdrant** | `127.0.0.1:6333` REST, `127.0.0.1:6334` gRPC | *none* | — | **No** |
| **personal-kb neo4j** | `127.0.0.1:7474` HTTP, `127.0.0.1:7687` bolt | `neo4j` / `…` (personal-kb's own) | `NEO4J_AUTH` | **Yes** |

All five ports bind **127.0.0.1 only** (loopback; verified via `ss -tlnp`).
No external exposure. The threat model is local-process, not network.

### 1.1 qdrant-dt — no auth

Qdrant 1.20 supports an optional `--api-key` (env: `QDRANT__SERVICE__API_KEY`)
that, when set, turns on a `X-API-Key` header requirement on every REST
request and a `grpc_auth_key` on the gRPC channel. In the dt-local
compose, neither is set: `qdrant-dt` runs bare, every REST endpoint
(`/collections`, `/health`, …) accepts unauthenticated requests.

The `digital-twins` app code already has the knob:
`qdrant.api_key` → env `KB_QDRANT__API_KEY`, default `None`.
All Qdrant client construction sites (`mcp/dispatch.py`, `web/app.py`,
`health.py`, `cli.py`, `scheduler/loop.py`) thread it through:

```python
QdrantClient(url=url, api_key=get(config, "qdrant.api_key") or None)
```

So **the app is ready** — it just has nothing to authenticate against
in this deployment.

### 1.2 neo4j-dt — auth in place, but `.env` is stale

`neo4j-dt` was created with `NEO4J_AUTH=neo4j/dt-neo4j`.
The `.env` at `~/.sops/dt-local-deploy/.env` still reads:

```
NEO4J_PASSWORD_DT=21f5905b636eab7c8f9e9eb4f18770c3
```

…which is the **original** random password. The running container
(`digital-tokens` on port 8770) was started with `NEO4J_PASSWORD_DT=dt-neo4j`.
The `.env` was not updated after the password was changed.
A `docker-compose up` from a clean `.env` would create a mismatch
(the new `.env` password would not match the running `neo4j-dt`).

`digital-tokens` env confirms it is using the correct `dt-neo4j`.
No functional gap, but the `.env` is a documentation / reproducibility risk.

### 1.3 digital-tokens (MCP :8770) — auth in place

`POST /mcp` with **no** `Authorization` header → **401**.
`POST /mcp` with a wrong token → **403** (known-but-dead → fail closed).
The three-credential order (service → personal → session) mirrors
003's `cli._auth_checker`. `DT_SERVICE_TOKEN` is **not set** in the
`digital-tokens` container's env — the container only runs with
`service_account_email="system"` and no shared service token.
A personal or session token is the actual auth path today.

### 1.4 personal-kb qdrant — no auth

Same posture as qdrant-dt: bare, no `--api-key`, loopback-only.
The `digital-tokens` app is not wired to personal-kb's qdrant
(`KB_QDRANT__URL` points at `:6335`), so this is personal-kb's
own concern, not the digital-twins app's.

---

## 2. The stdio MCP channel — trust surface

This is the most important architectural point for the trust model.

The stdio transport (`digital-twins serve-mcp --transport stdio`)
resolves the caller once, at process start:

```python
# stdio.py::_resolve_caller
if DT_SERVICE_TOKEN:
    → service_account_email   (default "system")
elif DT_PERSONAL_TOKEN:
    → verify_personal_token → that token's email
else:
    → service_account_email   (default "system")
```

Every request on the NDJSON stream then runs as that single caller.
There is **no per-request credential** on the stdio path in v1 —
the docstring is explicit:

> The per-request JSON does NOT carry credentials in v1: the stdio
> transport is a local, single-user channel (one process, one
> operator), so the process-owner credential is the caller's
> identity for every request.

This means:

- **Whoever launches the stdio process** (the human operator, or an
  `Hermes` cron job, or a CI runner) is the caller for every request.
- If the process runs under a **service account** (the default
  `service_account_email="system"` when no credential is present),
  then **every tool call on that stdio stream is attributed to the
  service account** — the audit log's `per_source_counts` will say
  the HTTP/stdio caller is `"system"`, not the human.
- There is **no way** for a downstream MCP client (e.g. a Claude Code
  or DSH agent connected over stdio) to present *its own* personal
  token on a per-request basis — the token is baked in at process
  start.
- `DT_SERVICE_TOKEN` is the strongest credential: a process that
  exports it runs as `service_account_email` (admin, by default)
  regardless of who the actual user is.

**Implication:** if a non-trusted process on the host launches
`serve-mcp --transport stdio` with `DT_SERVICE_TOKEN` set, it has
full admin MCP access to the digital-tokens state DB. The trust
boundary is *process-local*: only the process owner matters, not
any downstream consumer.

### 2.1 Is this a real risk?

For the current deployment: the stdio MCP is launched by `digital-tokens`
itself (the container's entrypoint), not by an external user. The
container's `digital-tokens` process is the trust boundary — same as
the `dt-tokens` service in the compose. An attacker who can launch
`python -m digital_twins.mcp` inside the container already has the
DB handle and can do more than any MCP tool.

**However**, if the stdio server is ever launched from the host
`digital-tokens` shell (the `docker run --rm -it digital-tokens:0.11.3
python -m digital_twins.mcp` pattern in the runbook), then the host
user *is* the trust boundary, and a `DT_SERVICE_TOKEN` env var would
elevate that host user to admin.

---

## 3. Findings summary

| # | Finding | Severity | Status |
|---|---|---|---|
| **A1** | `qdrant-dt` has no auth; any process on the host that can reach `:6335` can read/delete collections | Medium | Open |
| **A2** | `.env` `NEO4J_PASSWORD_DT` is stale (mismatch vs running container) | Low | Open |
| **A3** | `DT_SERVICE_TOKEN` is not set in the `digital-tokens` container; MCP auth is personal/session token only | Info | Intentional |
| **A4** | stdio MCP runs as the process owner; `DT_SERVICE_TOKEN` in env = admin for every request | Medium (by design) | Open — needs decision |
| **A5** | `personal-kb` qdrant (`:6333`) also has no auth | Medium | Out of scope (different project) |
| **A6** | All five services bind 127.0.0.1 only; no external exposure | Info | Acceptable |

---

## 4. Options & recommendations

### 4.1 qdrant-dt: enable API key

**Option Q1 (Recommended):** Set `QDRANT__SERVICE__API_KEY` on
`qdrant-dt` in the compose, and add the corresponding
`qdrant.api_key` to the `digital-tokens` env / `kb.local.yml`.

```yaml
# docker-compose.local.yml
qdrant-dt:
  environment:
    - QDRANT__SERVICE__API_KEY=${QDRANT_DT_API_KEY:?}
```

```yaml
digital-tokens:
  environment:
    - KB_QDRANT__API_KEY=${QDRANT_DT_API_KEY}
```

- Qdrant 1.20 supports this natively (`--api-key` flag or
  `QDRANT__SERVICE__API_KEY` env).
- The app's Qdrant client already threads `qdrant.api_key`
  through — no code change needed.
- `personal-kb`'s qdrant is a separate concern; this only touches
  the dt-deploy qdrant-dt.
- Cost: one env var, one compose line, one `kb.local.yml` key.

**Option Q2:** Leave qdrant-dt bare (status quo). Acceptable only
while the threat model is strictly "host-local, trusted user,
loopback-only". Not acceptable once a second user or a containerised
agent runs on the same host.

### 4.2 neo4j-dt: fix `.env`

**Option N1 (Recommended):** Sync the `.env`:

```bash
# .env
NEO4J_PASSWORD_DT=dt-neo4j
```

…to match the running container. No `neo4j-dt` restart needed
(the container already uses `dt-neo4j`); just keep the file and
the running state consistent.

**Option N2:** Keep the `.env` random-hex value, restart
`neo4j-dt` to apply it, and update `digital-tokens` env to match.
More disruptive, no security benefit over N1.

### 4.3 stdio trust boundary

**Option S1 (Recommended — document the boundary):** Keep the
stdio transport single-user, but make the **process owner**
the explicit trust decision. Document in the runbook:

> The stdio MCP server trusts the process owner. Do not export
> `DT_SERVICE_TOKEN` in the host shell unless the host user
> should be admin. The container's own `digital-tokens` process
> is the trust boundary; a host-launched stdio server inherits
> the host user's credential.

No code change. This is a runbook / ops decision.

**Option S2 (per-request personal token on stdio):** Extend the
stdio NDJSON protocol so each request can carry
`"auth": "<personal_token>"` and the handler resolves the caller
per-request. This is a spec change (feature 016+) and a code
change in `stdio.py`. Only needed if the stdio transport must
serve multiple concurrent callers with different roles.

**Option S3 (stdio → HTTP for multi-caller):** Route multi-caller
scenarios through the HTTP transport (`:8770`), which already has
per-request Bearer auth. The stdio transport remains the
local-single-operator path. No new code; just a deployment
guideline.

### 4.4 DT_SERVICE_TOKEN in the container

**Option T1 (Recommended):** Keep `DT_SERVICE_TOKEN` unset in the
`digital-tokens` container env. The container's auth is via
personal / session tokens (already in place). The service token
is a host-side credential for cross-container / service-to-service
MCP calls; it should not be baked into the container image.

**Option T2:** Set `DT_SERVICE_TOKEN` to a random value in the
`digital-tokens` container and share it only with trusted host-side
callers. Only if a host-side process needs to call the container's
MCP HTTP endpoint as the service account.

---

## 5. Residual risk table

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Host process reads qdrant-dt collections | Low (host-local, trusted user) | Medium (data exfil / delete) | Q1 (API key) |
| `.env` / running container drift on `neo4j` restart | Low | Low (service outage) | N1 (sync `.env`) |
| Host user launches stdio MCP with `DT_SERVICE_TOKEN` | Very low (runbook doesn't say to) | High (admin MCP) | S1 (document) |
| Second user or container agent added to the host | Unknown | High | Q1 + S1 + S3 |

---

## 6. Action items (checklist)

- [ ] **Q1**: Add `QDRANT__SERVICE__API_KEY` to `qdrant-dt` compose
      env; add `KB_QDRANT__API_KEY` to `digital-tokens` compose env
      and `kb.local.yml`. Regenerate a random value for both.
- [ ] **N1**: Update `~/.sops/dt-local-deploy/.env` →
      `NEO4J_PASSWORD_DT=dt-neo4j`.
- [ ] **S1**: Add a "Trust boundary" section to the dt-local-deploy
      runbook: stdio = process-owner trust; do not export
      `DT_SERVICE_TOKEN` in the host shell.
- [ ] **S3**: Note in the runbook that multi-caller MCP should use
      the HTTP transport (`:8770`), not stdio.

---

*Audit by: digital-tokens agent · 2026-06-16 ·
Host: `100.92.91.108` · Deployment: `~/.sops/dt-local-deploy/`*
