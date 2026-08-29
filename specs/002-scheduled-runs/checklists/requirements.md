# Requirements Checklist: Scheduled & On-Demand Runs

- [x] Requirement text is free of implementation details (no library names, no DDL)
- [x] Scope is clearly bounded (presets only; no cron parsing; no sign-up/roles/MCP)
- [x] Dependencies and assumptions identified (001 accounts/highwater/audit; A1–A6)
- [x] User stories are independently testable (US1–US5 each carry an Independent Test)
- [x] Acceptance scenarios are testable without implementation knowledge
- [x] Edge cases enumerated (kill, clock skew, dual instances, catch-up, same-minute race)
- [x] No contradictions with 001 invariants (portability, one-record-not-N, fail-fast)
- [x] Locked owner decisions cited, not re-litigated (Q6, Q10, NFR-1/9/14)
