# Tasks: dashboard-left-column

## 1. API routes (digital_twins/web/app.py)
- [ ] 1.1 RED: `tests/unit/test_web_dashboard_api.py` — in-process handler
      driver (mirror tests/unit/test_web_channels_api.py pattern) covering:
      - `GET /api/dashboard/accounts` 200 `{"accounts":[{email,role,
        created_at,last_active}],"total":N}`; `password_hash`/any
        credential key absent (FR-D3); 403 `permission_denied` for
        scheduler + reader (FR-D2).
      - `GET /api/dashboard/jobs` 200 `{"schedules":[...],
        "recent_runs":[...]}` most-recent-first; 403 non-admin.
      - `GET /api/dashboard/models` 200 effective model-knob view with
        `*_set` booleans, no credential values; 403 non-admin.
      - 401 unauthenticated on all three (bearer gate, FR-D1).
- [ ] 1.2 GREEN: add the three dispatch branches to
      `_dispatch_rest` + the three handlers (`_handle_dashboard_accounts`,
      `_handle_dashboard_jobs`, `_handle_dashboard_models`) in
      `digital_twins/web/app.py`, reusing `_require_admin`,
      `scheduler.schedules.list_schedules`, `state` audit tail, and the
      config-layer effective view (same four-layer precedence as the
      services view).

## 2. Dashboard left column (digital_twins/web/static/)
- [ ] 2.1 RED: `tests/unit/test_web_dashboard_ui.py` — static contract
      (mirror tests/unit/test_web_channels_ui.py): the `#dashboard-side`
      left column + the four panel ids (hidden by default, FR-D4), the
      panel fetch endpoints, the admin-gate co-location in
      `showKbView()`, the CSS two-column grid rule, and the FR-D3
      no-credential-value discipline.
- [ ] 2.2 GREEN: edit `index.html` (add the `#dashboard-side` column +
      four panel cards + loader JS + `showKbView()` unhide) and
      `style.css` (two-column grid + side-panel rules + narrow fallback).

## 3. Final gate
- [ ] 3.1 Standing guards green: `tests/integration/test_portability.py`
      (T006) + `tests/unit/test_knob_docs.py` (T027).
- [ ] 3.2 Regression: existing web static-contract tests stay green
      (`tests/unit/test_web_channels_ui.py`,
      `tests/integration/test_web_config_probe.py`,
      `tests/unit/test_web_app_kb.py`).
