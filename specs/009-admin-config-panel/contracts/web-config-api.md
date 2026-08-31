# Contract: Web Admin UI — Service Probe (009, FR-003..FR-005)

Extends the 008 config surface (`specs/008-service-hosting/contracts/web-config-api.md`). The two 008 routes are unchanged. The probe route requires role `admin` (resolved via the existing `accounts.get_role(db, caller_email)`; non-admin → 403; missing/invalid bearer → 401 — existing shapes).

## POST /api/config/services/probe

**Request** (JSON; body may be `{}` or omit `services`):
```json
{"services": ["qdrant"]}
```
- Omitted or `{}` → probe all four (`qdrant, neo4j, llm, embedding`).
- Each name must be one of the four hard services.

**200** (the requested subset, request order; all four → canonical order):
```json
{"services": {
  "qdrant": {
    "status": "unreachable",
    "detail": "unreachable: URLError: <urlopen error [Errno -2] Name or service not known>",
    "remediation": "check qdrant.url (env: KB_QDRANT__URL) points at a live Qdrant host:port"
  }
}}
```
- `status` ∈ `ok | unconfigured | unreachable | auth-failed` — exactly `digital_twins.health.VALID_STATUSES`, computed by the same `health.check_*` functions the CLI `validate` runs (FR-003: no duplicated probe logic).
- Per-service deadline (`PROBE_PER_SERVICE_DEADLINE_S`, 4.5 s): on expiry that service reports `status="unreachable"`, `detail="probe timed out after 4.5 s"`, `remediation` naming the url knob + env form. "Test all" against four down/hanging services returns all results in ≤ 5 s wall-clock total (FR-005 / SC-002) — checks run in parallel.
- `detail` / `remediation` never carry credential values (008 FR-004 scrubbing applies; 009 FR-006).

**Errors**:
- 400 `invalid JSON body` — body is not a JSON object.
- 400 `services must be a non-empty list` — `services` is present but is not a list, or is an empty list.
- 401 — missing/invalid bearer token (existing shape).
- 403 `permission_denied` — caller is not admin (existing shape).
- 404 `unknown service "<name>"` — `services` contains a name outside the four hard services.

**Semantics**:
- Read-only: no state-DB writes, no audit rows, no test points/collection creation, no config writes.
- The effective config is re-read per request (env > kb.local.yml > kb.yml > defaults) — a just-saved URL is probe-visible without a server restart.
- Probe results are transient: never persisted.
