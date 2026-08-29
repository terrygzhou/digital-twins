# Requirements Checklist: Multi-User Sign-Up, Roles & Per-User Surfaces

- [x] Requirement text is free of implementation details (no library names, no DDL)
- [x] Scope is clearly bounded (sign-up/roles/per-user surfaces only; no MCP scheduler tools (004), no Docker/PyPI (005), no hard isolation (Q2 follow-up))
- [x] Dependencies and assumptions identified (001 accounts/highwater/audit; 002 schedules/serve/status; A1–A5)
- [x] User stories are independently testable (US1–US5 each carry an Independent Test)
- [x] Acceptance scenarios are testable without implementation knowledge
- [x] Edge cases enumerated (duplicate email, last-admin demotion, reader mutating call, scheduler reconfig attempt, personal-token revocation, two users same source different caps)
- [x] No contradictions with 001/002 invariants (portability, one-record-not-N, fail-fast, R-12 owner-label semantics preserved)
- [x] Locked owner decisions cited, not re-litigated (Q2 soft isolation, Q3 three roles, Q8 simple sign-up, Q9 placeholder ACL, Q10 presets)
- [x] Clarify-stage ambiguities resolved (C-1 token table, C-2 role enforcement surface, C-3 per-user config precedence, C-4 first-account creation path, C-5 /status auth gating)
