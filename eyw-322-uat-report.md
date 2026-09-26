# EYW-322 — Ingestion Channel Config: UI + Persistence QA

Date: 2026-09-26 (AEST)
QA: CEO agent (delegated from QA pi_local / Qwen3.8-27B — that agent could not
complete the task because its model layer emitted the bash command as ```bash
markdown instead of a structured tool call; see "Root cause of the blocked QA run").

## Task
Deploy digital-twins locally and test the ingestion channel-setting surface
(UI + persistence), produce a report in the workspace.

## Method
Deployed the web server in an ISOLATED sandbox so the real config was never
touched:
- `KB_STATE_DIR=/tmp/eyw322-state KB_CONFIG_DIR=/tmp/eyw322-config KB_WEB__PORT=8791`
- backend: uvicorn via `.venv/bin/digital-twins web` (uvloop + httptools)
- entry point: `http://127.0.0.1:8791/`

## Results — PASS
| # | Check | Expected | Actual |
|---|-------|----------|--------|
| 1 | UI loads `GET /` | 200 + index | 200, 35,088 b |
| 2 | `index.html` renders channel-config panel | references channels | `channels-config`, `channels-enabled-`, `channels-env-overrides`, `channels-error` present |
| 3 | Unauthed channel GET/POST | 401 (NFR-17) | 401 / 401 |
| 4 | Sign up first account → admin | 200 | 200 (admin) |
| 5 | Admin channel GET | 200 + sources | 200, all 5 sources (fs/hermes/pi/dsh/…) |
| 6 | **Persistence**: admin POST `{"fs":{"enabled":true,"max_items":7}}` | 200 + write to config | 200; landed in `/tmp/eyw322-config/kb.local.yml` (`fs.enabled: true`, `max_items: 7`); **reload via GET returns the new value** |
| 7 | Reader (2nd account, non-admin) channel GET/POST | 403 (NFR-16 role-gating) | 403 / 403 |
| 8 | Negative value `max_items:-5` | 422 schema violation | 422 `sources.fs.max_items: must be >= 0, got -5` |
| 9 | Unknown knob `{"fs":{"bogus":1}}` | 404 | 404 `unknown source knob` |
| 10 | Config isolation (no real-config leak) | real `~/.config/digital-twins/kb.local.yml` untouched | no `max_items: 7` in real config ✓; write stayed in isolated dir |

## Findings / caveats
- **API body shape:** the channel POST body is a MAPPING keyed by source name
  (`{"fs": {...}}`), NOT `{"source":"fs",...}`. An `{"source":"fs"}` body 404s
  with "unknown source 'fs'". The UI panel uses the correct mapping shape;
  consumers building the call by hand should follow the mapping shape.
- **Auth route is `/api/auth/credentials`** (not `/api/auth/me`). The older
  `uat-report.md` referenced the stale `/me` route — update it if reused.
- Channel prerequisites surface correctly (e.g. `sources.pi` reports its
  missing `sessions_dir` prerequisite and recommends disable-or-set), which is
  the intended advisory behavior.

## Root cause of the blocked QA run (EYW-322)
The QA agent `pi_local` (Qwen3.8-27B, called directly against SGLang at
`http://localhost:8080/v1`, NOT litellm) never sets `toolChoice` in its agent
loop, and under `auto` the model drifted to describing the bash command in a
` ```bash ` markdown fence instead of emitting a structured tool call. pi then
settled the turn as healthy; Paperclip's watchdog saw "parked without
disposition", retried 2×, and escalated to the board → `blocked`. Three
heartbeat runs (`e188167a`, `b7d5b953`, `930aa86d`) all exit 0 and end in that
markdown fence with zero `toolCall`/`toolResult` blocks.

### Fix applied (user-directed: "force tool-calls on SGLang directly")
Patched pi's `openai-completions-EKZT2IH2.js` chunk to inject
`params.tool_choice="required"` on the FIRST model call of a turn when tools
are present (keeping `auto` on later turns so the agent can still finish with
a text answer). Verified end-to-end: the pi CLI now performs a real
structured `bash` round-trip and terminates cleanly. Backups:
`/tmp/openai-completions-EKZT2IH2.js.bak`, `~/.pi/agent/models.json.bak.force-tool`.
