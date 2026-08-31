# Quickstart Validation — 009 Web Admin Service Configuration & Health Panel

Prereqs: repo checkout; a Python env with the package dependencies (see AGENTS.md; e.g. `pip install -e .` or the project venv).

## 1. Test gates (fast)

```bash
pytest -q tests/integration/test_web_config_probe.py tests/unit/test_secret_hygiene.py
```
Expected: all new 009 tests green.

Full suite + standing guards:
```bash
pytest -q
```
Expected: green, including `tests/integration/test_portability.py` (T006) and `tests/unit/test_knob_docs.py` (T027).

## 2. Manual end-to-end (admin: view → edit → save → test → green, SC-001)

1. Start the web server with a scratch config dir:
   ```bash
   export KB_CONFIG_DIR="$(mktemp -d)/config"
   python -m digital_twins web        # default 127.0.0.1:8767
   ```
   (Knobs: `web.bind` / `web.port` / `web.base_url`; env forms `KB_WEB__BIND` etc.)
2. Open `http://127.0.0.1:8767` → sign up (the first account becomes admin).
3. The **Services** panel is visible (admin only) with four rows; each row shows the effective URL (or empty), `*_set` badges, a status pill, and an env-override indicator when `KB_*` vars shadow a knob.
4. Edit a row's URL (e.g. qdrant → `http://127.0.0.1:6333`), click **Save** → the panel re-renders from the post-write view; `kb.local.yml` under `$KB_CONFIG_DIR` contains the new value, unrelated keys preserved.
5. Click **Test** on that row → pill turns `ok` (or `unreachable` / `auth-failed` with remediation text naming the exact knob + env var).
6. Point a URL at a dead port (e.g. `http://127.0.0.1:1`) → **Test** shows `unreachable`; with all four URLs dead, **Test all** returns all four results in ≤ 5 s (time the click → all pills updated, SC-002).

## 3. Non-admin regression (US3)

Create a second account (non-admin) via the CLI or `accounts` module, sign out / sign in as that user:
- The Services panel is not rendered in the DOM.
- `curl -s -X POST -H "Authorization: Bearer <reader-token>" http://127.0.0.1:8767/api/config/services/probe -d '{}'` → `403 {"error":"permission_denied", ...}`.

## 4. Secret hygiene (SC-003)

With all four services configured with credentials, **Test all** and inspect the response body and the web server log: no credential value appears anywhere (automated by the probe case in `tests/unit/test_secret_hygiene.py`).
