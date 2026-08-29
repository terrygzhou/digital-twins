# 006 Web App — Requirements Quality Checklist

**Feature**: `specs/006-web-app`
**Status**: complete

## [x] No placeholder text ([FEATURE NAME], [DATE], [SECTION]) remaining
- spec.md, research.md, data-model.md, contracts/, checklists/, quickstart.md all written;
  no `[FEATURE NAME]` / `[DATE]` / `[SECTION NAME]` / `[PRIORITY]` placeholders remain.

## [x] Every functional requirement (FR-###) is testable
- Each FR maps to an Acceptance Scenario and/or a success criterion:
  - FR-001/002/014/017 → US-1 scenarios 1–5 + SC-001/004
  - FR-003 → US-1 / `GET /api/me` contract
  - FR-004 → US-1 scenarios 1–4
  - FR-005 → US-2 scenarios 1–3
  - FR-006 → US-3 scenarios 1–3
  - FR-007/008/009 → US-4 scenarios 1–5 + SC-002/004
  - FR-010 → US-5 scenarios 1–3 + SC-005
  - FR-011 → US-6 scenarios 1–3 + C-1/R8
  - FR-012 → C-8 / SC-003
  - FR-013 → C-6
  - FR-015 → US-1–US-6 + SC-006
  - FR-016 → C-7 / NFR-13

## [x] No non-testable "vague" requirements (e.g. "fast", "nice", "better")
- All requirements use MUST/verifiable language; "first-class" is grounded in
  concrete acceptance (reachable from another machine, sign-in, query,
  trigger — BR-11.1.8) rather than a subjective adjective.

## [x] Each user story has at least 3–5 Given/When/Then acceptance scenarios
- US-1: 5, US-2: 3, US-3: 3, US-4: 5, US-5: 3, US-6: 3.

## [x] Edge cases are identified and specific (not "handles errors well")
- Edge Cases section: bind address, fresh-install sign-up, concurrency,
  session expiry/revocation, no enabled sources, unknown source name,
  Qdrant down — each concrete.

## [x] Key entities described with enough detail to design the data model
- Account, Session, KB Point, Audit Run, Config Knobs — all described with
  fields and role; data-model.md adds the reuse boundary and request/response
  shapes.

## [x] Success criteria are measurable and not aspirational
- SC-001..SC-006 each have a concrete, checkable outcome (second-machine sign-in,
  dedup parity, three knobs in lock-step, 401/403 fail-closed, per-user audit,
  static UI drives /api/* only).

## [x] Requirements are grounded in the binding brief
- BR-11.1.8 (first-class surface), BR-11.4.1 (email+password sign-in),
  NFR-13 (portability), NFR-16 (per-user audit), NFR-17 (credential scoping),
  NFR-1/NFR-14 (dedup parity), Q8 (open sign-up), Q10 (no cron) all referenced
  and locked; Q1–Q10 not re-litigated.

## [x] Rulings recorded and not left open in the spec
- R1–R8 (incl. R6 = (a) new `web/app.py` wraps `server.py`; R7 port 8767;
  R8 chat = surface only) are recorded in spec.md Rulings and elaborated in
  research.md (C-1..C-8). None are marked "open" without a ruling.

## [x] No host path / username / install location in the spec (NFR-13)
- All examples use `<host>`, `localhost`, `tmp_path`, `KB_STATE_DIR`; no
  `/home/<user>`, no username, no install location.
