# Quickstart: Onboarding an Agent to the MCP Scheduler Tools (004)

How an MCP-capable agent (Claude Desktop, Cursor, pi, DSH, Hermes, …)
onboards to `digital-twins` and calls each of the six scheduler tools over
**stdio** and **HTTP/SSE (Streamable HTTP)**. No host-specific setup beyond a
token (BR-11.5.2); the canonical onboarding doc is
`docs/references/agent-guides.md` (FR-3).

## 0. Prerequisites (the host operator, one-time)

The MCP server is an **optional extra** (research D1). On a host that already
has the 001/002/003 install (state DB migrated, an account created):

```bash
# add the MCP surface
pip install "digital-twins[mcp]"

# start the server (choose a transport)
digital-twins serve-mcp --transport stdio          # stdio (agent spawns it)
# — or —
digital-twins serve-mcp --transport http --port 8770   # HTTP/SSE (default 127.0.0.1)
# (equivalently: python -m digital_twins.mcp --transport http --port 8770)
```

- The HTTP bind address + port are config knobs (default `127.0.0.1`,
  `mcp.port` default `8770`) — widen the bind address only if the agent is on
  another machine (NFR-13: no host value is hardcoded).
- The state DB path is resolved through the config layer (NFR-13), never a
  host path.

### Get a token
| Credential | How | Grants |
|---|---|---|
| **Personal token** (recommended) | `digital-twins token create` (admin/scheduler self-service; shows once) | that user's role |
| **Session token** | web UI `POST /signin` → `session_token` | that user's role, 8 h TTL, revocable |
| **Service token** (BR-10) | `DT_SERVICE_TOKEN` env on the server | the configured service account |

The agent's token identifies **who** the caller is; role-gating (R4) and
owner-scoping (R5) then apply per call.

## 1. stdio onboarding

The agent (or its harness) **spawns** the server as a child process and speaks
JSON-RPC over stdin/stdout. The token is passed via the `DT_MCP_TOKEN` env var
in the spawned process (research D3).

### Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "digital-twins": {
      "command": "digital-twins",
      "args": ["serve-mcp", "--transport", "stdio"],
      "env": { "DT_MCP_TOKEN": "<personal-or-service-token>" }
    }
  }
}
```
(Cursor / pi / DSH / Hermes use the same shape: `command` + `args` +
`DT_MCP_TOKEN` env, or the equivalent spawn mechanism.)

### Manual JSON-RPC smoke test
```bash
export DT_MCP_TOKEN="<token>"
digital-twins serve-mcp --transport stdio <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kb_schedule_list","arguments":{"agent_kind":"curl"}}}
EOF
```
`tools/list` returns the six scheduler tools + four stubs. `tools/call`
returns the envelope from `contracts/scheduler.md`.

## 2. HTTP/SSE (Streamable HTTP) onboarding

Point any MCP client at the server URL with a `Bearer` token:
- **URL:** `http://127.0.0.1:8770/mcp` (the SDK's Streamable-HTTP endpoint;
  SSE streams server→client, HTTP POST carries client→server).
- **Auth:** `Authorization: Bearer <token>` header on every request (D3).

### Claude Desktop (HTTP)
```json
{
  "mcpServers": {
    "digital-twins": {
      "url": "http://127.0.0.1:8770/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```
(A clients that only support stdio should use the stdio form above; the tool
set and audit are identical either way — R7.)

### Manual curl smoke test
```bash
# list tools
curl -s -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -H "Accept: text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://127.0.0.1:8770/mcp
```

## 3. Calling each tool

All calls carry the optional `agent_kind` (the client's declared kind, e.g.
`"hermes"`; omitted → `"unknown"`, R6). Responses are the envelopes in
`contracts/scheduler.md` (`ok:true,data:{...}` or `ok:false,error:{code,message}`).

### `kb_schedule_list` — list schedules (own; admin can pass `all_users`)
```json
{"name":"kb_schedule_list","arguments":{"agent_kind":"hermes"}}
{"name":"kb_schedule_list","arguments":{"all_users":true,"agent_kind":"admin-cli"}}
```
→ `data.schedules[]` with `schedule_id/owner/source/preset/param/fire_time/
enabled/next_fire_at/acl/created_at/updated_at`. A non-admin with
`all_users:true` → `permission_denied`.

### `kb_schedule_create` — create a schedule (owner = caller, v1)
```json
{"name":"kb_schedule_create","arguments":{
  "source":"hermes","preset":"daily","fire_time":"03:00",
  "enabled":true,"agent_kind":"hermes"}}
```
`every-N-hours` requires `param` (int):
```json
{"name":"kb_schedule_create","arguments":{
  "source":"gmail","preset":"every-N-hours","param":6,
  "fire_time":"00:00","agent_kind":"hermes"}}
```
→ `data.schedule`. Errors: `invalid_preset`, `invalid_param`, `conflict`
(duplicate), `permission_denied` (reader).

### `kb_schedule_update` — change a schedule
```json
{"name":"kb_schedule_update","arguments":{
  "schedule_id":12,"enabled":false,"agent_kind":"hermes"}}
```
→ `data.schedule`. Errors: `schedule_not_found`, `invalid_preset`,
`invalid_param`, `permission_denied`.

### `kb_schedule_delete` — remove a schedule
```json
{"name":"kb_schedule_delete","arguments":{"schedule_id":12,"agent_kind":"hermes"}}
```
→ `data.deleted`. Errors: `schedule_not_found`, `permission_denied`.

### `kb_schedule_run` — trigger a one-shot run now (synchronous)
```json
{"name":"kb_schedule_run","arguments":{"schedule_id":12,"agent_kind":"hermes"}}
```
Runs the **same 001 `run_pipeline`** every other trigger path uses
(research D4): `trigger="mcp"`, `scheduled_by=<caller>`, owner-tagged points.
Returns when the run completes:
→ `data` = `{ "run_id", "status", "per_source_counts", "agent_kind" }`.
Errors: `schedule_not_found`, `source_disabled`, `run_failed` (run audited as
`failed`), `permission_denied`. **Exactly one** `audit_runs` row is written
(`trigger='mcp'`, `agent_kind` recorded) — this is the row the
one-record-not-N check (NFR-1/NFR-14) dedups against CLI/schedule/web-UI runs.

### `kb_run_history` — fetch audit records (own; admin can target another user)
```json
{"name":"kb_run_history","arguments":{"agent_kind":"hermes"}}
{"name":"kb_run_history","arguments":{"since":"2026-08-01T00:00:00Z","limit":50,"agent_kind":"hermes"}}
```
→ `data.runs[]` with `run_id/started_at/completed_at/status/trigger/
scheduled_by/per_source_counts` + `data.count`.
- **Own** (no `user`, or `user == caller`): the normal "mine" view; **no**
  audit row is written.
- **Admin cross-user** (`user` = another email, admin role): returns that
  user's runs **and writes one access-log row** (R8):
  `status='ok'`, fresh `run_id`, `trigger='mcp'`, `scheduled_by=<admin>`,
  `per_source_counts = {"mcp_history_query": {"target_user": "<email>",
  "agent_kind": "<kind>"}}`. A non-admin requesting another user's history →
  `permission_denied`.

### The four BR-10 stubs (registered, not implemented — R10)
```json
{"name":"kb_search","arguments":{"agent_kind":"hermes"}}
```
→ `{"ok":false,"error":{"code":"not_implemented_yet",
"message":"kb_search is a BR-10 tool, a follow-up slice; not implemented in 004"}}`
Same for `kb_chat`, `kb_ingest`, `kb_health`. They appear in `tools/list` so a
fresh client sees a complete registry; they do nothing.

## 4. Role cheat-sheet (what each role can do, R4)

| Tool | reader | scheduler | admin |
|---|:---:|:---:|:---:|
| `kb_schedule_list` (own) | ✓ | ✓ | ✓ |
| `kb_schedule_list` (`all_users`) | ✗ | ✗ | ✓ (R9) |
| `kb_schedule_create` | ✗ `permission_denied` | ✓ | ✓ |
| `kb_schedule_update` | ✗ `permission_denied` | ✓ | ✓ |
| `kb_schedule_delete` | ✗ `permission_denied` | ✓ | ✓ |
| `kb_schedule_run` | ✗ `permission_denied` | ✓ | ✓ |
| `kb_run_history` (own) | ✓ | ✓ | ✓ |
| `kb_run_history` (cross-user) | ✗ | ✗ | ✓ (writes access-log row, R8) |

> A reader *can* list their own schedules and view their own history, but is
> denied every mutating tool with `code=permission_denied` naming the missing
> capability (US1 S4).

## 5. Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| `unauthorized` on every call | token missing/unknown — check `DT_MCP_TOKEN` (stdio) or the `Authorization` header (HTTP); confirm the token is a live personal/session/service token |
| `permission_denied` naming a capability | caller's role lacks it (reader hitting a mutating tool) — promote the account, or use a scheduler/admin token |
| `schedule_not_found` for a schedule that "exists" | owner-scoping (R5): the caller neither owns it nor is admin — expected isolation, not a bug |
| `source_disabled` on `kb_schedule_run` | the schedule's source is off in config — enable it (config layer), then re-run |
| `run_failed` | the pipeline ran and failed; the run is audited as `failed` with `trigger='mcp'` — inspect `per_source_counts` / structured logs |
| `conflict` on `kb_schedule_create` | duplicate `(owner,source,preset,param,fire_time)` — the 002 UNIQUE constraint |
| server won't start | missing MCP extra — `pip install "digital-twins[mcp]"`; or port in use / bad bind address |

## 6. What 004 does *not* cover (follow-ups)
- `kb_search` / `kb_chat` / `kb_ingest` / `kb_health` — BR-10, registered but
  stubbed (`not_implemented_yet`).
- Per-schedule ACL *enforcement* — `schedule.acl` is shipped and accepted but
  not enforced in v1 (R5); owner-scoped by default.
- Custom cron / arbitrary recurrence — presets only (Q10); host-cron
  `digital-twins run --once` is the escape hatch.
- Hard per-user data isolation — shared KB + owner tags in v1 (Q2).
