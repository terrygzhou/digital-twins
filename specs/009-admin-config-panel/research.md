# Research & Decisions — 009 Web Admin Service Configuration & Health Panel

## D1 — Probe route: `POST /api/config/services/probe`

**Decision**: A new admin-gated sub-route under the existing 008 config namespace, dispatched in `_dispatch_rest` by exact path (same style as the sibling routes) and gated by the existing `_require_admin` helper.

**Rationale**:
- POST (not GET): the probe triggers live network I/O and takes a body (`{"services": [...]}`) selecting the subset; `_read_json_body` and the POST handler pattern already exist on this route table.
- Same namespace: the panel and the route share the admin gate, the error shapes (400/401/403/404), and the config-view semantics; keeping the probe under `/api/config/services/*` keeps the "one config surface" story and the 404 `unknown service` shape consistent.
- `GET /api/config/services` stays a pure read (masked view) — no probe piggyback, so the 008 contract is untouched.

**Alternatives considered**:
- `GET /api/health/services` — rejected: a second admin-gated namespace, splits the config surface, and the panel is config-centric.
- Probe as query params on GET — rejected: long-running I/O on a safe method + awkward list encoding in the query string.

## D2 — Probe logic: reuse `health.check_*`, not `preflight`

**Decision**: The probe handler calls the four existing per-service check functions (`health.check_qdrant` / `check_neo4j` / `check_llm` / `check_embedding`), resolved by module-attribute lookup at request time, and maps each `HealthResult` to `{status, detail, remediation}`.

**Rationale**: FR-003 forbids duplicated probe logic. The CLI `validate` (`cli.py:164`) runs `health.run_health_checks(load())` — the same four functions. `preflight` raises on the first non-ok service, so it cannot report all four; the web route calls the per-service functions directly (identical check code, no fork).

**Alternatives considered**:
- `preflight` per service in try/except — rejected: re-implements the loop; `ServiceDependencyError` strings are the CLI gate shape, not the per-service dict shape.
- A lighter duplicate TCP-connect probe — rejected: spec violation (FR-003) and divergent semantics (CLI says ok, web says unreachable).

**Testability note**: the handler resolves check functions by name at request time (`getattr(health, "check_" + service)`), so integration tests can monkeypatch `digital_twins.health.check_*` to deterministic fakes (local `http.server` for llm/embedding, sleeps for the budget test) with no real qdrant/neo4j.

## D3 — Time budget (FR-005 / SC-002: Test-all ≤ 5 s against four hanging services)

**Decision**: Run the requested checks in parallel on a `concurrent.futures.ThreadPoolExecutor(max_workers=len(services))`; await each future with a per-service deadline — module constant `PROBE_PER_SERVICE_DEADLINE_S = 4.5` in `web/app.py`. On deadline, synthesize that service's result: `status="unreachable"`, `detail="probe timed out after 4.5 s"`, `remediation` naming the service's url knob + env form. With four parallel checks, total wall-clock ≈ 4.5 s ≤ 5 s.

**Rationale**: the existing check functions use their own socket timeouts (`HTTP_TIMEOUT_S = 10` for urllib; qdrant_client/neo4j driver defaults), so sequential execution against four hanging services would take ≥ 40 s. Parallel + hard per-service deadline meets the 5 s budget without touching shared `health.py` constants (which the CLI and 001–007 rely on).

**Alternatives considered**:
- Sequential with 1.2 s per service — rejected: the total is fine but a 1.2 s per-service budget flake-risks slow-but-alive endpoints (LAN latency, cold LLM servers).
- Lowering global `HTTP_TIMEOUT_S` / passing client timeouts — rejected: changes CLI `validate`/`init` behavior (principle VI; scope creep).
- New knob `web.probe_deadline_s` — rejected for v1: the spec fixes the budget (≤ 5 s); a knob would add T027 knob-docs + schema surface for a value that does not need to vary. Revisit in a follow-up feature if needed.

**Known limitation (documented, v1)**: a timed-out check thread is not killable; it lingers until its own socket timeout fires (urllib: 10 s; qdrant/neo4j: client defaults). It is bounded, harmless to the response (already sent), and acceptable for a single-admin local tool.

## D4 — Probe request/response contract

**Decision**:
- **Request body**: `{}` (probe all four) or `{"services": ["qdrant", ...]}` — a non-empty list of a subset of `qdrant|neo4j|llm|embedding`.
  - Body not a JSON object → 400 `invalid JSON body` (existing shape).
  - `services` present but not a list, or an empty list → 400 `services must be a non-empty list`.
  - `services` containing an unknown name → 404 `unknown service "<name>"` (008 POST 404 shape).
- **200 response**: `{"services": {<name>: {"status", "detail", "remediation"}}}` — exactly the requested subset, in request order (all four → canonical order `qdrant, neo4j, llm, embedding`).
- **Errors**: 401 missing/invalid bearer; 403 `permission_denied` (non-admin) — existing shapes.
- **Config re-read**: the handler loads the effective config fresh per request (`config.loader.load(config_dir=..., env=os.environ)`), exactly like the 008 GET/POST handlers — a saved URL is probe-visible without a restart (spec assumption "no restart required").
- **Status values**: exactly `health.VALID_STATUSES` — no new status vocabulary (FR-003 "the same information the CLI renders").

## D5 — UI: panel wiring in the existing single-page app

**Decision**: a new `<section class="card services-panel" id="services-panel" hidden>` inside `#kb-view` (below the chat panel), plus inline JS in the existing IIFE:
- `bootstrap()`: once `me` resolves, if `me.role === "admin"` → unhide the panel + load the masked view via `GET /api/config/services`; otherwise the panel element stays `hidden` (not rendered — SC-004).
- Row per service (canonical order): service name, URL `<input>` prefilled with the effective value, `*_set` credential badges (booleans only), env-override badge (`env_overrides` vars mapped to the service via the `KB_<SERVICE>__` prefix), status pill (initial `not tested`), remediation line (when not ok), per-row **Test** button, per-row **Save** button, and a **Test all** toolbar button.
- **Save**: POST `/api/config/services` with `{"<service>": {"<knob>": value}}` using the display→knob mapping `qdrant→url`, `neo4j→url`, `llm→endpoint`, `embedding→endpoint` (the 008 GET view flattens `*.endpoint` to the UI key `url`; 008 POST tests confirm the knob names, e.g. `{"llm": {"endpoint": ...}}`). On 200, re-render the panel from the post-write view (the POST response itself is the post-write masked view — FR-002). On 4xx, show the error in `#services-error` and keep the input values (FR-008: no lost edits).
- **Test / Test all**: POST `/api/config/services/probe`; a per-service in-flight flag suppresses duplicate probes (US2 AC5) and disables the button mid-flight; "Test all" is one request with all four names.
- **Env-override warning**: when a row's service has a shadowing `KB_*` var, the row shows "env override in effect: `KB_…`" and a hint that saving does not change the effective value until the var is unset (spec edge case).
- **Clearing**: no clear/delete control (clarify session: overwrite-only). Emptying the input and saving writes `""` (schema `_ALLOW_EMPTY`), which is the de-facto clear path.

**Alternatives considered**:
- A separate `main.js` file — rejected: 006 deliberately ships inline JS in index.html; consistency over file hygiene.
- A global "Save all" button — rejected: per-row save keeps POST bodies minimal (one section at a time), matches the "any subset" contract, and limits 422 blast radius.

## D6 — Secret hygiene (FR-006 / SC-003)

**Decision**: probe responses are built only from `HealthResult.detail` / `remediation` (already scrubbed in `health.py` — the URL query-string strip covers credentials-in-URL); the handler adds no new user-visible strings and logs no request/response bodies. `tests/unit/test_secret_hygiene.py` gains a case: probe all four services with all four obviously-fake credentials configured; assert no credential value appears in the probe response JSON or in captured log records.

## D7 — No new knobs, no new dependencies

No `schema.py` change → the T027 knob-docs guard is unaffected. No `pyproject.toml` dependency change (stdlib only). No state-DB migration (no persistent entities).
