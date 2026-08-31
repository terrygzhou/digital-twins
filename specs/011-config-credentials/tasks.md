# Tasks: 011 — Web UI service credential fields (write-only)

**Input**: `specs/011-config-credentials/plan.md` (spec: `spec.md` — the authority).

**Tests**: included — constitution III (Test-First, NON-NEGOTIABLE):
RED commit failing before the GREEN commit, per task.

## Task 1 — RED: 011 credential-UI tests

- [ ] T1 RED: `tests/integration/test_web_config_credentials_ui.py` —
  (a) static contract: served index.html carries `SERVICE_CREDENTIALS`,
  the five credential knob names, `service-cred`, `type="password"`,
  the `"plain"` exception marker, and the `saveCredValues` helper
  marker; (b) Node-evaluated shipped `saveService`: blank creds
  omitted from the POST body, non-empty creds included under their
  knob names, whitespace-only treated as blank;
  (c) `renderCredInputs` never pre-fills from the GET view and sets
  an aria-label; (d) API round-trip: POST the neo4j user+password
  pair → 200, `user_set`/`password_set` true, zero values in the
  response, `kb.local.yml` gains the values; POST url-only leaves
  saved credentials intact (overwrite-only); reader POST → 403.
  Record the RED output in the ledger (expected: (a)+(b-includes)+(c)
  fail; (d) may already pass — the API half shipped in 008).

## Task 2 — GREEN: index.html credential inputs + save-omission

- [ ] T2 GREEN: `digital_twins/web/static/index.html` ONLY (FR-004 —
  `web/app.py` untouched): `SERVICE_CREDENTIALS` map +
  `renderCredInputs(service, row)` (empty inputs, `type="password"`
  except neo4j.user `"plain"` → text, aria-label, appended after the
  URL input) + `saveCredValues(service, inputs)` (trimmed, non-empty
  only) wired into the Save listener and `saveService(service,
  input, credInputs)`; `.service-cred` CSS rule; panel hint updated
  to state the write-only / blank-keeps-current behavior.
  Verify: the 8 new tests green + 009/008 regressions
  (`test_web_config_probe.py`, `test_web_config_api.py`) green.

## Task 3 — Verify: guards, full suite, live redeploy

- [ ] T3 VERIFY: standing guards green
  (`tests/integration/test_portability.py`,
  `tests/unit/test_knob_docs.py`); full suite (expect 976);
  reinstall into `/tmp/dtkb-pypi` and restart the :8767 server
  (bind 0.0.0.0; NEVER kill the ~30GB GPU process); live
  `GET /` carries `SERVICE_CREDENTIALS`; surface owner re-test
  instructions (neo4j user+password → Save → Test, SC-001).
