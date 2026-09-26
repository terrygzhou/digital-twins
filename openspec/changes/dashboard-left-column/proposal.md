# Change: Dashboard left column — account, ingestion channels, batch jobs, model updates

## Why
The KB view is a single stacked column of cards (knowledge base, chat,
services, channels). The dashboard surface promised by BR-11.1.8
(dashboard, query, chat, ingestion triggers, account management, run
history) has no at-a-glance overview: an admin cannot see account
statuses, which ingestion channels are active, which batch jobs
(schedules + recent runs) are in flight, and which models are
configured, without opening each admin panel.

This change re-lays-out the dashboard page: a left column of four
compact, read-only overview panels (account, ingestion channels, batch
jobs, model updates) next to the main column (knowledge base + chat +
existing admin config panels). The new panels are overview surfaces only;
they do not replace or move the existing mutation panels. All four are
admin-gated (SC-004 discipline, co-located with the services / channels
panels).

## What changes
- **New API routes (admin-gated, `digital_twins/web/app.py`)** — all
  under the existing bearer gate (401 unauthenticated), admin-gated
  (403 `permission_denied` for scheduler/reader) via the shared
  `_require_admin` helper:
  - `GET /api/dashboard/accounts` → `{"accounts": [{email, role,
    created_at, last_active}], "total": N}` — the accounts table minus
    credential material (no `password_hash`).
  - `GET /api/dashboard/jobs` → `{"schedules": [scheduler.schedules
    .list_schedules rows], "recent_runs": [last N audit_runs rows,
    most recent first]}` — schedules (the batch-job registry) plus the
    tail of the audit table (the run history). Admin sees all.
  - `GET /api/dashboard/models` → the effective view of the
    model-family knobs: `llm.{endpoint,model}`,
    `embedding.{model,device,endpoint}`, `qdrant.url`, `neo4j.url` —
    same four-layer precedence as the services panel, credential
    values reduced to `*_set` booleans (FR-004).
- **New static layout (`digital_twins/web/static/index.html` +
  `style.css`)**: the `#kb-view` main grid gains a left column
  (`#dashboard-side`) holding four compact cards:
  - **Account panel** (`#dashboard-account-panel`, `hidden` by
    default): the account list + role badge per account.
  - **Ingestion channels panel** (`#dashboard-channels-panel`): the
    active/disabled channel names from `GET /api/config/channels`,
    compact read-only list.
  - **Batch jobs panel** (`#dashboard-jobs-panel`): schedules (preset +
    next fire + enabled) and the last few audit runs.
  - **Model updates panel** (`#dashboard-models-panel`): the effective
    model knobs with `*_set` credential flags.
- **CSS**: `#kb-view` becomes a two-column grid (side column + main
  column); the side column stacks above the main column on narrow
  viewports.

## FR numbering (new, scoped to this change)
- FR-D1: `GET /api/dashboard/*` routes exist under the bearer gate
  (401 unauthenticated), mirroring the existing `/api/*` dispatch.
- FR-D2: `GET /api/dashboard/{accounts,jobs,models}` are admin-gated
  (403 `permission_denied` for scheduler/reader) via `_require_admin`.
- FR-D3: no credential value (`password_hash`, API keys, passwords)
  ever reaches any dashboard route payload or the static UI (FR-004
  discipline extended to the dashboard surface).
- FR-D4: the four dashboard panels ship `hidden` by default in the
  markup; each panel un-hide sits behind the `me.role === "admin"`
  gate in `showKbView()` (SC-004).
- FR-D5: the dashboard side panels are read-only overview: no save /
  write UI. Mutation stays on the existing services / channels panels
  and CLI.

## Non-goals
- No account-management mutations (create/demote/delete remain CLI /
  API work for a later slice).
- No schedule CRUD in the dashboard (schedules stay on CLI + MCP).
- No changes to the existing services / channels admin write panels
  (their contracts and static assertions are untouched).
- No bundler / framework; the UI remains static assets served by the
  existing `ThreadingHTTPServer` app (006 constraint A2-style).

## Impact
- `digital_twins/web/app.py` (dispatch + 3 handlers),
  `digital_twins/web/static/index.html` + `style.css` (layout + panels),
  `tests/unit/test_web_dashboard_api.py` (new, API contract),
  `tests/unit/test_web_dashboard_ui.py` (new, static contract).
