# Implementation Plan: Web Admin — Service Configuration & Health Panel (009)

**Branch**: `main` | **Date**: 2026-08-31 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/009-admin-config-panel/spec.md`

## Summary

008 shipped the admin REST surface for service config (GET/POST `/api/config/services` + atomic `kb.local.yml` writes via `config.local_io.merge_write`), but the static web UI never calls it and no live connectivity probe exists anywhere on the web surface. This feature (1) adds an admin-gated **Services panel** to the existing no-framework single-page UI (view/edit/save effective URLs for the four hard services, `*_set` credential flags, env-override indicators, remediation text), and (2) adds an admin-gated **`POST /api/config/services/probe`** route that reuses the existing `health.check_*` logic (the same checks the CLI `validate` runs) to return per-service `status ∈ {ok, unconfigured, unreachable, auth-failed}` + remediation, bounded so "Test all" against four down/hanging services completes in ≤ 5 s wall-clock. No new persistent entities, no new config knobs, no new dependencies, no UI framework.

## Technical Context

**Language/Version**: Python ≥ 3.11 (package `requires-python`); host dev/test on 3.12 (host pin — not a package pin).

**Primary Dependencies**: stdlib only for new code (`http.server` dispatch, `urllib`, `concurrent.futures`, `json`). `qdrant_client` / `neo4j` remain lazily imported inside the existing `health.check_*` functions — nothing new to install.

**Storage**: No new storage. Writes stay on `config.local_io.merge_write` → `kb.local.yml` (machine-local, gitignored). Probes are read-only: no state-DB writes, no new table, no migration.

**Testing**: pytest, mirroring `tests/integration/test_web_config_api.py` (tmp_path state DB, `build_web_app` on `127.0.0.1:0`, `http.client` requests). Deterministic probes via local `http.server` endpoints + monkeypatched `health.check_*` + real sleeping threads for the budget test. Standing guards: `tests/integration/test_portability.py` (T006), `tests/unit/test_knob_docs.py` (T027), and `tests/unit/test_secret_hygiene.py` (008 FR-004 — extended to the probe route).

**Target Platform**: Any host where the package runs (macOS via pipx, Linux); web server binds to the `web.bind`/`web.port` knobs (default 127.0.0.1:8767).

**Project Type**: library + CLI + in-package web service (`http.server`-based, no web framework).

**Performance Goals**: FR-005 / SC-002 — "Test all" against four down/hanging services returns complete per-service results in ≤ 5 s wall-clock total. Mechanism: parallel checks, per-service deadline constant `PROBE_PER_SERVICE_DEADLINE_S = 4.5`.

**Constraints**:
- Admin-gated probe route (403 `permission_denied`, existing shape).
- Credential values never returned or logged (008 FR-004 extended by 009 FR-006 / SC-003).
- No new config knobs (T027 knob-docs guard stays green); no new third-party deps; no host paths/usernames in shipped code or UI assets (NFR-13 / T006).
- UI remains the framework-free single-page app (`index.html` + `style.css`, inline JS) per 006.

**Scale/Scope**: 1 new route + 1 UI panel + 1 CSS block; 4 services; single-machine deployment.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Verdict | Notes |
|---|---|---|
| I. Portability & Environment Neutrality | PASS | No host paths, usernames, or install locations in new code/UI/docs; everything resolves through the config layer (`local_config_path`, `config_dir` knob); tests use `tmp_path` / `127.0.0.1` only. |
| II. Deterministic, Idempotent Ingestion | PASS (N/A) | No ingest-path change; probes are read-only and write nothing. |
| III. Test-First (NON-NEGOTIABLE) | PASS (enforced) | RED-first tests for all new behavior: probe route (new integration file), SC-002 budget, 400/401/403/404 shapes, secret-hygiene extension, UI DOM presence. Implementation lands only after RED. |
| IV. Config-First, Fail-Fast | PASS | No new knobs; probe re-reads the effective config per request via `config.loader.load` (env > kb.local.yml > kb.yml > defaults), so a saved URL is probe-visible without restart; invalid input surfaces as existing 400/404/422 shapes. |
| V. Auditability & Observability | PASS | Probe results are transient (no audit rows by design — spec "Transient state only"); request logging keeps the existing `log_message` masked-view behavior; errors keep existing shapes. |
| VI. Upgrade Safety & Versioning | PASS | Purely additive: one new sub-route + one UI section; existing route semantics unchanged; version bump to 0.9.0 in the polish phase. |

Gate result: **PASS — no violations; Complexity Tracking not required.**

## Project Structure

### Documentation (this feature)

```text
specs/009-admin-config-panel/
├── spec.md                # Feature spec (009, clarify session 2026-08-31)
├── plan.md                # This file
├── research.md            # Phase 0 output
├── data-model.md          # Phase 1 output
├── quickstart.md          # Phase 1 output
├── contracts/
│   └── web-config-api.md  # Probe-route contract (extends the 008 config contract)
└── tasks.md               # Phase 2 output (from /speckit-tasks)
```

### Source Code (repository root)

```text
digital_twins/
├── web/
│   ├── app.py                     # + POST /api/config/services/probe (dispatch + handler:
│   │                              #   admin gate, per-request config re-read, parallel bounded probe)
│   └── static/
│       ├── index.html             # + Services panel section (admin-gated, hidden by default)
│       │                          #   + inline JS: load masked view, per-row Save, Test / Test all
│       └── style.css              # + service-row / status-pill styles
tests/
├── integration/
│   └── test_web_config_probe.py   # NEW: probe route (200 shape, 400/401/403/404, SC-002 budget, UI DOM)
└── unit/
    └── test_secret_hygiene.py     # EXTEND: probe responses + log records never carry credential values
```

**Structure Decision**: No new modules or directories. The probe handler lives next to the existing `/api/config/services` handlers in `digital_twins/web/app.py` (shared admin gate + config-view helpers stay adjacent); the panel lives in the existing single-page static UI (006's no-framework decision). New tests get a new integration file (008's `test_web_config_api.py` stays untouched) plus an extension of the existing 008 secret-hygiene unit test.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

None — Constitution Check passed on all six principles; no justification table required.
