# Research: MCP Scheduler Tools for Any Agent (004)

Phase 0 output. Resolves every NEEDS-CLARIFICATION item and the five
plan-stage technical decisions against the shipped 001/002/003 codebase.
All decisions reuse shipped machinery where it exists (001 pipeline, 002
scheduler + state, 003 accounts/auth/roles) and add new modules only where
none exist. No open items remain.

Locked rulings honored (NOT re-opened): R1 (build the full MCP server from
scratch: registry + stdio + HTTP/SSE + BR-10.5 token auth reusing 003
`auth_checker`/`verify_personal_token`/`verify_session`; the six scheduler
tools; BR-10 search/chat/ingest/health out of scope), R2 (tool schemas mirror
003 `contracts/scheduler.md` field names exactly), R3/R8 (admin cross-user
history read = one `audit_runs` row), R4 (role-gating per 003 R3), R5
(caller-scoped default; `can_access_schedule` swappable; `schedule.acl`
already in 002 schema — consume, don't add), R9 (admin "list all" rides on the
`admin` role directly; no new `view_all_schedules` capability; 11-cap matrix
frozen), R10 (BR-10 `kb_search`/`kb_chat`/`kb_ingest`/`kb_health` registered
but stubbed, `code=not_implemented_yet`), R6 (`agent_kind` = declared kind or
`unknown`), R7 (stdio + HTTP/SSE identical tool set + identical audit).

## D1 — MCP library: the official `mcp` SDK (`modelcontextprotocol/python-sdk`),
installed as an optional extra

**Decision.** Use the official Python SDK, import name `mcp` (PyPI distribution
`mcp`, `modelcontextprotocol/python-sdk`), pinning a v1.x series. It is declared
as an **optional extra**, not a core dependency:

```toml
# pyproject.toml
[project.optional-dependencies]
mcp = ["mcp>=1.2,<2"]
dev = ["pytest>=8.0"]
```

`pip install digital-twins` stays lean (no MCP surface); `pip
install "digital-twins[mcp]"` adds the server. A plain core install still
imports the `digital_twins` package normally — the MCP module
(`digital_twins/mcp/`) is only imported when the extra is present or when the
`serve-mcp` entry point is invoked (a missing import is a clean, named error,
not a crash: *"MCP extras missing — run `pip install digital-twins[mcp]`"*).

**Transport support.** The official SDK ships **both** required transports out
of the box, which is the whole point of R7:
- `mcp.server.stdio.StdioServer` — `mcp_server()` for stdio (agents that spawn
  a local process: Claude Desktop, Cursor, the host's own agent harness).
- `mcp.server.streamable_http` + an ASGI app — the **Streamable HTTP**
  transport. This is the HTTP-side transport in the MCP spec; it delivers SSE
  server→client streaming and HTTP POST client→server in one mechanism. It
  satisfies "HTTP/SSE" in BR-10.6 without a separate legacy-SSE server.

One library therefore provides the *identical* tool registry over both
transports (D2), which is what makes transport parity (R7) a property of the
build rather than two hand-kept-in-sync copies.

**Python fit.** `requires-python = ">=3.11"` (001/BR-11.2.6). The `mcp` SDK
requires Python ≥3.10; 3.11 is comfortably inside the range, and the package's
own declared range is the only supported-Python guarantee (constitution
additional constraint: the host's pinned interpreter is not an assumption).

**Why the official SDK over `fastmcp`.**
- `fastmcp` (jlowin/fastmcp) is a convenient decorator layer that re-exports
  the same underlying `mcp` SDK transports underneath. It adds an API we would
  then have to keep out of sync with our auth/audit path, and its opinionated
  server lifecycle is a second code path to audit for the one-record invariant
  (constitution II). We only need the protocol *primitives* (stdio +
  streamable-HTTP + the JSON-RPC server loop) plus our own auth/audit wrap —
  exactly the official SDK's surface, with zero extra abstraction.
- The official SDK is the reference implementation of the spec version we
  target, so the wire format matches what Claude Desktop / Cursor / pi / DSH /
  Hermes already speak.
- Keeping the dependency an **extra** means a `pip install digital-twins`
  user (CLI-only, the 001/002/003 path) pays nothing for MCP, and the core
  package's hard-dependency list is unchanged — a host-portability and
  dependency-minimalism win (constitution I/IV).

**Alternatives considered.**
- **`fastmcp`** — rejected (above): adds a framework layer over the same SDK,
  a second audit/par surface for no v1 benefit.
- **Hand-rolled JSON-RPC over `ThreadingHTTPServer` + a stdio loop** — rejected:
  re-implementing the MCP wire protocol (initialize handshake, tool
  enumeration, progress/partial results) is a large, spec-coupled surface that
  would silently drift from client expectations; the official SDK is the
  single source of truth for the protocol.
- **A different HTTP transport (legacy HTTP+SSE)** — rejected: the spec has
  folded SSE into Streamable HTTP; the official SDK's `streamable_http` is the
  forward-compatible choice and is what current clients negotiate.

## D2 — Transport binding: ONE tool-registry + ONE audit path, two thin
adapters (parity by construction)

**Decision.** A single module `digital_twins/mcp/registry.py` owns
*everything* that is transport-independent:

1. **`MCPContext`** — the authenticated caller. Built once per request/session
   by the shared auth path (D3): `{"db": <conn>, "caller_email": str,
   "caller_role": str, "agent_kind": str}`.
2. **`build_tool_registry() -> list[Tool]`** — the ten tool definitions (six
   real + four stubs, R10), each a `Tool(name, description, inputSchema)` plus
   a uniform handler `handler(ctx, args) -> dict | ToolError`. The *schema*
   of each tool (field names, types) is generated from the 003
   `contracts/scheduler.md` shapes (R2) so the agent-facing contract cannot
   drift from the 003 contract.
3. **`dispatch(ctx, tool_name, args) -> dict | ToolError`** — the single
   executor. It does, in order:
   - role-gate the tool (R4, via `accounts.require_capability`),
   - owner-scope the target (`can_access_schedule`, R5/R9),
   - run the tool body,
   - write the audit row (R3/R8),
   - return the result dict (R2 field names).

   Every transport calls **this one function**. There is no per-transport
   tool body, no per-transport audit call, no per-transport role check.

**The two adapters** (each ~50 lines, transport-only):
- `digital_twins/mcp/stdio.py` — `run_stdio()`: opens a state connection
  (config-resolved path, NFR-13), then loops `mcp`'s stdio server; each
  incoming `tools/call` JSON-RPC is mapped to: authenticate from the **stdio
  session**'s token (env `DT_MCP_TOKEN` or the token the client sent in
  `initialize` params — see D3), build the `MCPContext`, call
  `dispatch(ctx, name, args)`, serialize the `dict`/`ToolError` back to the
  MCP result/error envelope.
- `digital_twins/mcp/http.py` — `run_http()`: builds the official SDK's
  Streamable-HTTP ASGI app with a Starlette (or bare-ASGI) auth middleware.
  Each HTTP request: authenticate from the `Authorization: Bearer <token>`
  header (D3), build the `MCPContext`, the ASGI handler calls the **same**
  `dispatch(ctx, name, args)`, and the result is wrapped in the SDK's MCP
  response. No tool logic lives here.

**Entry points.**
- Console script: `digital-twins serve-mcp --transport stdio|http [--port N]`
  (a new `click` subcommand in `cli.py`, mirroring the existing `serve`).
  `--transport stdio` → `run_stdio()`; `--transport http --port 0` picks a free
  port (config-resolved default `mcp.port`, mirroring 002 `scheduler.status_port`).
- `python -m digital_twins.mcp` (a `__main__.py` in the `mcp/` package) accepts
  the same flags, so an agent can `command: python -m digital_twins.mcp,
  args: ["--transport","stdio"]` with no installed console script.
- The HTTP server binds a configurable address (default `127.0.0.1`, BR-8.8 /
  NFR-13) and the configurable `mcp.port` knob (constitution IV: it is a
  documented knob, not a host constant).

**Why this guarantees R7.** The parity invariant "stdio and HTTP/SSE expose the
identical tool set + identical audit" holds because *there is one* tool set
(`build_tool_registry`), *one* executor (`dispatch`), and *one* audit path
(D4/D5). The two adapters differ only in (a) how they receive the request and
(b) how they read the bearer token. A regression test (SC-001, the
"contract test per transport") asserts both transports return byte-identical
`dispatch` output for the same `ctx` + `args` — the test passes by
construction because both call the same function.

**Alternatives considered.**
- **Two separate tool modules (one per transport)** — rejected: that is how
  R7 drifts; the spec explicitly forbids duplication.
- **A generic ASGI app for *both* transports** — rejected: stdio is not HTTP;
  forcing stdio through ASGI adds a socket to a process the agent spawns as a
  child. The SDK's native stdio server is the correct primitive.

## D3 — Auth wiring: reuse 003's `auth_checker` shape; resolve the caller's
email + role per request

**Decision.** Each MCP request authenticates exactly like 003's
`_auth_checker` (003 `contracts/scheduler.md` "003 addition 1" + `research.md`
R8), but with one 004 extension: the checker must return **who** the caller is
(not just allow/deny), because role-gating (R4) and owner-scoping (R5) need the
caller's `email` + `role`, not just a `bool | str`.

New module `digital_twins/mcp/auth.py`:

```python
def mcp_authenticator(db) -> callable:
    """Bearer-token auth for the MCP server (BR-10.5, 004).

    Returns `authenticate(headers: dict[str, str]) -> tuple[bool, str|None]`:
      (True,  email)  -> allow; the caller is the account `email`.
      (False, None)   -> 401: no / unknown credential.
      (False, reason) -> 401/403: known credential, unusable.

    Credential order (identical to 003 _auth_checker):
      1) shared service token (BR-10)   — DT_SERVICE_TOKEN env, compare_digest.
         NOTE: the service token is a *non-account* credential. For the MCP
         surface it authenticates the caller as the **service account**
         (email = config `mcp.service_account_email`, a documented knob,
         default "system"); its role is whatever that account holds.
      2) personal token — auth.verify_personal_token(db, token) -> email | None
      3) session token  — auth.verify_session(db, token) -> email | None
    """
```

- **Reuse, don't re-implement.** `verify_personal_token` and `verify_session`
  (003, `digital_twins/auth.py`) are the credential verifiers; the service
  token is the same `DT_SERVICE_TOKEN` + `hmac.compare_digest` pattern 003's
  `_auth_checker` uses. 004 does not add a fourth credential type.
- **DB access.** The authenticator and every tool body open the state DB the
  same way 002/003 servers do: `_open_same_db(db, check_same_thread=False)`
  (the 002/003 pattern, `scheduler/status.py` + `web/server.py`) so the
  connection is usable from the handler/worker thread and `PRAGMA
  foreign_keys=ON`. A per-request (or per-session) connection is serialized by
  a lock, exactly as `StatusServer`/`WebServer` do.
- **Role resolution.** After the email is known, `accounts.get_role(db, email)`
  yields the role; that `(email, role)` pair + the `agent_kind` (D6) form the
  `MCPContext`. A credential that verifies to an *unknown* account (row
  deleted) fails closed → 401 (never serve a tool call on an auth error,
  mirroring 003's "fail closed" handler behavior).
- **Stdio credential source.** stdio has no HTTP headers. The token is taken
  from, in order: (a) the `DT_MCP_TOKEN` env var in the spawned process, (b)
  an optional `token` field in the client's `initialize` params. The same
  `mcp_authenticator` is called with `{"Authorization": f"Bearer {token}"}` so
  the code path is *literally* the one above (parity with HTTP by construction,
  R7).

**Why `bool|str` is not enough here (and how we stay compatible).** 003's
`auth_checker` returns `True`/`str`/`False` because `/status` only needs
allow/deny. 004's `mcp_authenticator` returns `(bool, email|None)` because the
MCP surface *is* the role-gated mutating surface. This is additive: 003's
`auth_checker` is untouched and still gates `serve /status` + the web UI; 004
introduces a new, richer checker for the MCP transport. The 003 "403 reserved
for future mutating routes" note is now *that* surface.

**Alternatives considered.**
- **Reuse the 003 `auth_checker` object directly** — rejected: its
  `bool|str` contract cannot express the caller's identity, and 004 needs it
  for R4/R5. A wrapper that re-resolves the email after a `True` would do two
  DB lookups per request; the new `(bool,email)` checker is the minimum that
  carries the identity.
- **A JWT-signed token** — rejected: 003 locked revocable, server-side
  credentials (R4: "a JWT cannot be revoked without a denylist, which is
  exactly the table we are adding"). MCP uses the same personal/session/service
  tokens.

## D4 — `kb_schedule_run` pipeline parity: call the SAME `run_pipeline` so
one-record-not-N holds

**Decision.** `kb_schedule_run` does **not** build a new ingestion path. It
resolves the target schedule (owner-scoped, R5), merges that owner's
`user_config` overrides into a copy of the global config via 003's
`merge_user_config` (exactly as `serve_once_tick` does), and calls the **same**
001 `run_pipeline` that every other trigger path uses:

```python
# digital_twins/mcp/registry.py — kb_schedule_run body (illustrative)
merged = merge_user_config(config, db, schedule["owner"], source=schedule["source"])
run_pipeline(
    merged, db, qdrant_factory, embedder,
    source_names=[schedule["source"]],
    trigger="mcp",                 # BR-11.5.3
    scheduled_by=ctx["caller_email"],  # the *caller*, not the schedule owner
    owner=schedule["owner"],        # R6: owner-tag the points (caller == owner in v1)
)
```

**Why this preserves the invariant.** 001's `run_pipeline` is the single
dedup authority: deterministic point IDs (`prefix|item_key|chunk|hash`,
`ingest/ids.py`) + per-source high-water marks (`state.models.upsert_highwater`)
+ the `assert_dimension` guard. `owner` is a **payload-only** field (003 R6) —
it never enters the point ID, so "the same content ingested via schedule, via
`run --once`, via MCP, or via web UI yields **one** point, not four" (NFR-1,
NFR-14, top acceptance check in AGENTS.md) holds because all four paths call
*this* function. The MCP path is one more trigger under BR-4, exactly as
`trigger="schedule"` (002 serve) and `trigger="manual"` (CLI `run --once`) are.

**Audit parity (R3/R4/R6).** `run_pipeline` writes its own `audit_runs` row via
`start_audit_run`/`finish_audit_run` with `trigger="mcp"` +
`scheduled_by=<caller>` — the *only* audit row for the run (constitution V: one
row per run). `agent_kind` is stamped on that row by the MCP layer (the
pipeline's `finish_audit_run` does not know `agent_kind`); the MCP layer sets it
in the *same* transaction window (an `UPDATE audit_runs SET ... WHERE
run_id=<the run's id>`) immediately after `run_pipeline` returns — see D5.

**Synchronous vs. fire-and-forget.** `kb_schedule_run` runs the pipeline
**synchronously** inside the tool call and returns the run record
(`run_id`/`status`/`per_source_counts`) when it finishes. This keeps the audit
row complete at response time (SC-004: "exactly one audit row with
`trigger='mcp'` + `agent_kind`"). Long runs are acceptable — the MCP tool call
carries the progress the client needs; the one-record invariant is independent
of duration. (A background-queue variant is a documented follow-up; it would
still call `run_pipeline`, so parity is preserved.)

**Alternatives considered.**
- **Call `serve_once_tick`** — rejected: that fires *all* due schedules for
  *their* owners and advances their `next_fire_at`; `kb_schedule_run` is a
  *targeted* one-shot on a specific schedule owned by (in v1) the caller, with
  `trigger="mcp"` and `scheduled_by=<caller>` — semantically a manual run, not a
  serve-tick fire. Reusing `serve_once_tick` would (a) advance the schedule's
  clock (wrong for a manual trigger), (b) misattribute `scheduled_by` to the
  schedule owner instead of the caller, and (c) couple the MCP path to the
  serve loop's pidfile/signal machinery.
- **A new MCP-specific pipeline** — rejected: that is a second ingestion path
  and the exact defect the one-record invariant forbids (constitution II).

## D5 — The audit-row shapes (R3/R8): the two distinct `audit_runs` writes

There are **two** audit writes in 004, and they must not be conflated:

1. **`kb_schedule_run` (a real ingestion run).** `run_pipeline` writes the row
   (`trigger="mcp"`, `scheduled_by=<caller>`, `status` ok/partial/failed,
   `per_source_counts` = the real per-source counts). The MCP layer then sets
   `agent_kind` on that same row. One run = one row (the pipeline owns it).

2. **`kb_run_history` admin cross-user read (R8).** A *read*, not an ingestion.
   It writes **one** `audit_runs` row that is an *access-log* event, not a run:
   - `status="ok"` (the CHECK allows `ok`/`partial`/`failed`; a read that
     returns is `ok`).
   - `run_id` = **a fresh `uuid.uuid4()`** per read (a read does not collide
     with any run's `run_id`).
   - `trigger="mcp"`.
   - `scheduled_by=<admin actor's email>`.
   - `per_source_counts` = JSON encoding the target, e.g.
     `{"mcp_history_query": {"target_user": "<email>", "agent_kind": "<kind>"}}`.

   A caller's *own*-history query writes **no** such row — it is the normal
   `list_runs(user=...)` view. One read = one row; repeated reads = repeated
   rows (documented: it is an access log, not an ingestion log). This closes
   the "who looked at whose history" gap (constitution V) without touching the
   one-record-not-N invariant (a read is not an ingestion).

**`agent_kind` (R6).** Free-text provenance, value = the client's declared
kind or `"unknown"`. It is accepted as an **optional top-level tool argument**
(`agent_kind: str | None`) on every audited tool (the MCP client may declare
its kind — e.g. `"hermes"`, `"claude-desktop"` — in the call). Default
`"unknown"` when absent. It is stamped on (a) the `kb_schedule_run` audit row
and (b) the `kb_run_history` cross-user read row, and returned in the tool
response's provenance block. It is a payload/provenance field, not a dedup key
(same NFR-1 property as `owner`).

**Alternatives considered.**
- **No audit row for the admin read** — rejected: constitution V ("admins may
  query all" must be observable) + R8 explicitly lock the access-log row.
- **A separate `access_events` table** — rejected (data-model decision, DM1):
  the spec locks the row into `audit_runs` with a synthetic `run_id`; adding a
  table is the "new-table" path the data-model forbids absent a real need, and
  `audit_runs` already has every column R8 needs.

## D6 — `agent_kind` provenance: where the declared kind travels

**Decision.** `agent_kind` is *not* a new table column and *not* a new
top-level audit column — it rides in the **`per_source_counts` JSON** of the
audit row (the existing `TEXT` JSON column), consistent with how 002/003 already
pack structured run metadata there. For a `kb_schedule_run` run the MCP layer
merges `{"agent_kind": "<kind>"}` into the run's `per_source_counts` JSON at
finish; for the `kb_run_history` cross-user read it is inside the
`{"mcp_history_query": {"target_user": ..., "agent_kind": ...}}` object (R8).
This keeps the `audit_runs` **schema unchanged** (no migration, constitution
VI / NFR-15) while still recording provenance (constitution V).

**Alternatives considered.**
- **A new `audit_runs.agent_kind` column** — rejected: that is a schema change
  + migration; the JSON column already exists and already carries structured
  per-run data. The cost (a JSON parse on read) is negligible for a local-KB
  tool and the read path (`list_runs` / the MCP `kb_run_history` body) already
  parses `per_source_counts`.
- **A header-echoed value only (not persisted)** — rejected: constitution V
  requires the record be *queryable*; provenance must survive the request.

## D7 — Tool-registry data model (consumed, not created)

No new tables. 004 **consumes** the shipped schema:
- `schedules` (002 DDL_V2) — `id`, `owner`, `source`, `preset`, `param`,
  `fire_time`, `enabled`, `next_fire_at`, **`acl`** (already `TEXT NOT NULL
  DEFAULT 'owner'`), `created_at`, `updated_at`, plus the
  `UNIQUE(owner, source, preset, param, fire_time)` constraint. 004 *reads*
  and *writes* this table via 002's `scheduler/schedules.py`
  (`create_schedule`/`list_schedules`/`update_schedule`/`delete_schedule`).
- `audit_runs` (001 DDL_V1) — read by `kb_run_history` via 003's
  `list_runs`; written by `run_pipeline` (real runs) and by the
  cross-user-read access-log row (R8).
- `accounts` / `personal_tokens` / `sessions` (001/003 DDL) — read-only for
  004 (auth + role resolution).

The only **new** helper 004 introduces is `can_access_schedule(schedule_row,
caller_email, caller_role) -> bool` (R5/R9) — a pure function, no I/O, no
table. It returns `True` iff `schedule_row["owner"] == caller_email` **or**
`caller_role == "admin"` (R9: admin's "list all" rides on the `admin` role
directly; the 11-capability matrix is frozen — no `view_all_schedules` cap is
added). A future per-schedule ACL replaces this function's *body* (the
`schedule.acl` column is already shipped), not its call sites. See
`data-model.md` for the full entity/field mapping.

## Complexity Tracking (constitution: deviations require justification)

No constitution deviation. Every decision reuses a shipped mechanism:
- D1/D2: one registry + one executor (reduces, not adds, audit surface).
- D3: reuses 003's verifiers + 002's `_open_same_db`.
- D4: reuses 001 `run_pipeline` + 003 `merge_user_config` (the invariant is
  *preserved* by construction, not re-implemented).
- D5/D6: reuses the `audit_runs` JSON column; no schema change (constitution
  VI / NFR-15 upgrade safety honored — 004 adds **zero** migrations).
- D7: consumes 001/002/003 tables; the only new artifact is a pure predicate.

The one "new" code (the MCP module itself) is *required* by R1 (there is no MCP
server in this repository yet); it is the scaffolding, and its shape is chosen
to be the thinnest possible layer over the existing, audited, one-record-safe
machinery.

## NEEDS-CLARIFICATION resolution map (all closed)

| Item | Resolution | Decision |
|---|---|---|
| MCP library | official `mcp` SDK, optional extra, stdio + streamable-HTTP | D1 |
| Transport binding | one registry + one executor, two adapters, parity by construction | D2 |
| Auth | `mcp_authenticator` → (bool,email); reuses 003 verifiers + 002 `_open_same_db`; role via `accounts.get_role` | D3 |
| `kb_schedule_run` parity | same 001 `run_pipeline`, `trigger="mcp"`, `scheduled_by=<caller>`, owner-tagged points | D4 |
| Audit row shapes | run row (pipeline-owned) + cross-user-read access-log row (R8); `agent_kind` in JSON | D5 |
| `agent_kind` storage | inside `per_source_counts` JSON; no new column | D6 |
| Data model | consume 001/002/003 tables; `can_access_schedule` pure predicate; no new tables | D7 / data-model.md |
| BR-10 stubs | four registered tools, `code=not_implemented_yet` | R10 (contracts) |
| `agent_kind` value | client-declared or `"unknown"` | R6 |
| admin list-all | rides on `admin` role via `can_access_schedule`; no new cap | R9 |
