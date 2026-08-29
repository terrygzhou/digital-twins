# Requirements Checklist: MCP Scheduler Tools for Any Agent

- [x] Requirement text is free of implementation details (no MCP library names, no DDL; transport library explicitly deferred to plan/research)
- [x] Scope is clearly bounded (six scheduler tools + MCP server scaffolding only; `kb_search`/`kb_chat`/`kb_ingest`/`kb_health` out of scope, A2/R1; granular ACL + hard isolation are follow-ups)
- [x] Dependencies and assumptions identified (002 schedule store + shared pipeline + `acl` column; 003 accounts/personal-tokens/roles + `auth_checker`/`require_capability`; A1–A4)
- [x] User stories are independently testable (US1–US4 each carry an Independent Test)
- [x] Acceptance scenarios are testable without implementation knowledge
- [x] Edge cases enumerated (reader denied all mutating tools, cross-user isolation by default, admin all-user history, `acl` column present-but-unenforced, transport parity, machine-readable error codes)
- [x] No contradictions with 001/002/003 invariants (portability, one-record-not-N, fail-fast, R3 role matrix, `acl` column already in 002 DDL_V2)
- [x] Locked owner decisions cited, not re-litigated (Q9 placeholder ACL / owner-scoped v1, BR-10.5 token auth, BR-11.5.3 audit parity, BR-11.5.4 transport-agnostic)
- [x] Clarify-stage ambiguities resolved (c1 tool schemas mirror `contracts/scheduler.md`, c2 admin cross-user history audit-logged; locked rulings R1–R7: server built from scratch, role-gating mapping, caller-scoped default + swappable check, `agent_kind`, transport parity)
