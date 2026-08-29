# Implementation Plan: Multi-User Sign-Up, Roles & Per-User Surfaces

**Input**: spec at `specs/003-multi-user/spec.md`; 001/002 artifacts as the
compatibility baseline. Build on 001's `accounts` / `auth.authenticate` /
`auth.hash_password` and 002's scheduler (`serve` / `run --once` /
`schedule` / `/status`).

## Summary

003 adds multi-user sign-up, the three-role model (admin/scheduler/reader),
per-user workspaces (config overrides + run history + personal tokens), and
sign-in on every surface (web UI, HTTP API, MCP, CLI), with v1 soft data
isolation via owner tags. The slice is deliberately thin: it reuses 001's
account/auth machinery and 002's pipeline/scheduler, adding only the identity
tables (v3 migration), the role guard, the credential middleware, and the
owner-tag stamp. The pipeline stays a pure function (constitution II): the
role check lives at the HTTP/CLI boundary (C-2), the per-user config merge
lives at the caller (C-3), and the owner tag is a new additive `run_pipeline`
kwarg (R6) — none of these change `run_pipeline`'s core behavior for existing
callers.

Out of scope (later slices): hard per-user isolation (Q2 follow-up), a rich
web dashboard (A2: the UI is minimal — sign-in, schedule view, run history),
MCP session-credential plumbing beyond the "same credentials accepted"
contract (004 owns the MCP surface), Docker/PyPI (005).

## Technical Context

| Layer | Choice |
|---|---|
| Language | Python ≥ 3.11 (001 `requires-python`; no new pin) |
| Auth | 001 `auth.authenticate` (password) + new `personal_tokens` / `sessions` lookups (R1/R2/R4) |
| Role guard | thin middleware/guard at the HTTP/CLI route layer (C-2); `ROLE_CAPS` matrix (R3) |
| State | 001 SQLite store + migration v3 (`personal_tokens`, `user_config`, `sessions` + 2 `accounts` columns); additive-only |
| CLI | `click` (001 dependency); new `signup`/`account`/`token`/`config` groups; `run --as` + `schedule` gain role checks |
| Web UI | minimal: `/signup` + `/signin` return session tokens (R4); no SPA framework decision (A2) |
| Config | no new 001 config knob; per-user overrides are DB-backed (`user_config`, C-3/R5), merged at the caller |
| Owner tag | `run_pipeline` gains optional `owner=` kwarg → Qdrant payload `owner` + `owner_tag` (R6) |
| New dependencies | **none** |

## Architecture decisions (C-1..C-5)

- **C-1 — Personal token schema.** A separate `personal_tokens` table
  (`id`, `account_email` FK, `token_hash` UNIQUE, `created_at`,
  `last_used_at`, `revoked`). Rationale: a user may hold multiple tokens
  (browser session + API key + MCP token), revocation of one must not affect
  the others (US3 S3), and keeping `accounts`' shape unchanged means 001's
  `authenticate()` works without modification. The shared service token
  (BR-10) is a separate, static credential checked *before* personal-token
  lookup; personal tokens are additive, not a replacement.
- **C-2 — Role enforcement surface.** Role checks live at the HTTP/API route
  layer (a thin guard) for all mutating endpoints. The pipeline (`run_pipeline`)
  does **not** check roles — it trusts the caller to have passed the gate.
  The CLI's `run --as` (002) is an owner label (R-12); 003 extends it: after
  authentication succeeds, the CLI verifies the caller's role permits the
  action **before** writing audit rows. The web UI's sign-in returns a session
  token (R4); subsequent requests carry it and the same guard gates them.
- **C-3 — Per-user config: storage + precedence.** `user_config` table
  (`account_email`, `source`, `key`, `value`, `updated_at`,
  UNIQUE(account_email, source, key)) — a KV store of per-user, per-source knob
  overrides. Precedence: `user_config(U, source, key)` > global config
  resolution. The pipeline receives a *merged* config dict (global config with
  the owner's overrides applied), so `run_pipeline` is unchanged — the merge
  happens at the caller (serve tick or CLI `run --as`). This keeps the
  pipeline a pure function (constitution II) and avoids a role/user parameter
  in `run_pipeline`'s signature.
- **C-4 — First-account creation.** `init` creates the first admin account
  (after the state DB is migrated; prompts for email+password or reads
  `INIT_ADMIN_EMAIL`/`INIT_ADMIN_PASSWORD`). The web UI's `/signup` creates
  subsequent accounts as readers. Both share the same helper
  (`create_account`, role = "first row in accounts? → admin, else reader").
- **C-5 — `/status` auth gating.** `StatusServer.__init__` gains an optional
  `auth_checker(request_headers) -> bool | str` callable. The serve CLI passes
  a checker accepting (a) the shared service token or (b) a valid personal /
  session token; no credential → 401. Backward-compatible: existing callers
  that don't pass `auth_checker` get 002's no-auth behavior unchanged.

## File-level changes

### New files

```
digital_twins/
├── auth.py                 # + token/session credential helpers (R2/R4/R8)
│                           #   (verify_personal_token, verify_session,
│                           #    create_personal_token, create_session,
│                           #    revoke_*) — additive to the 001 password API
├── accounts.py            # NEW — account CRUD + role model
│                           #   create_account, next_role_for, get_role,
│                           #   set_role, delete_account, count_admins,
│                           #   last_admin_guard, ROLE_CAPS (R3)
├── user_config.py         # NEW — per-user config KV + merge (R5)
│                           #   set_override, get_overrides, merge_user_config
└── web/                   # NEW — minimal UI credential endpoints (A2/R4)
    ├── __init__.py
    └── server.py          # /signup + /signin (session tokens) + a thin
                           #   status passthrough; reuses StatusServer + the
                           #   C-5 auth_checker
tests/
├── unit/
│   ├── test_accounts.py        # NEW — account CRUD + last-admin guard (R7/R3)
│   ├── test_personal_tokens.py # NEW — token create/verify/revoke (R2)
│   ├── test_sessions.py        # NEW — session create/verify/expire/revoke (R4)
│   ├── test_roles.py           # NEW — ROLE_CAPS matrix, every row (SC-001)
│   ├── test_user_config.py     # NEW — merge precedence + isolation (R5/SC-003)
│   ├── test_auth_checker.py    # NEW — C-5 checker contract (R8)
│   └── test_owner_tag.py       # NEW — run_pipeline(owner=) payload (R6/SC-005)
└── integration/
    ├── test_multi_user_upgrade.py # NEW — v2→v3 data preservation
    └── test_owner_isolation.py    # NEW — two users' points, owner filter (SC-005)
```

### Modified files

```
digital_twins/
├── state/models.py        # + DDL_V3 (apply_v3): personal_tokens, user_config,
│                           #   sessions, + accounts.created_at/last_active
├── state/migrations.py    # + (3, apply_v3) in MIGRATIONS; SCHEMA_VERSION -> 3
├── ingest/pipeline.py     # + run_pipeline(owner=None) kwarg; payload gains
│                           #   "owner"/"owner_tag" when set (R6)
├── scheduler/status.py    # + StatusServer(auth_checker=None) + do_GET wraps the
│                           #   check when present (C-5/R8)
├── scheduler/loop.py      # serve_once_tick merges the schedule owner's
│                           #   user_config before run_pipeline; passes owner=
│                           #   to run_pipeline (C-3/R5/R6)
├── cli.py                 # init: first-admin step (C-4/R7); run --as + schedule:
│                           #   post-auth role check (C-2); + signup/account/token/
│                           #   config groups; serve: passes auth_checker to StatusServer
└── auth.py                # (additive) token/session helpers alongside 001's
                           #   password API
docs/
└── (003 adds a multi-user doc; host-neutral, NFR-13)
```

> **Backward-compat invariant**: no 001/002 call site breaks. `run_pipeline`
> gains an optional kwarg (default `None` = legacy); `StatusServer` gains an
> optional kwarg (default `None` = 002 behavior); `accounts` gains two columns
> with defaults; `auth.authenticate` is untouched. 001's portability +
> one-record guards (SC-006) re-run at completion.

## Dependencies (what lands before what)

```
Phase 1 (Foundational — migration + accounts + roles)
  1. migration v3 (data-model.md)            <- nothing (001 db unchanged)
  2. accounts.py CRUD + last_admin_guard     <- (1)
  3. ROLE_CAPS + test_roles matrix (SC-001)  <- (2)  [needs the role values]
  4. personal_tokens + auth token helpers    <- (1)
  5. sessions + auth session helpers         <- (1)

Phase 2 (Identity surfaces — sign-in everywhere)
  6. C-5 auth_checker + StatusServer hook    <- (4),(5)  [checker looks up tokens/sessions]
  7. cli signup/account/token/config groups  <- (2),(3),(4),(10)
  8. run --as + schedule post-auth role check <- (3)   [guard uses ROLE_CAPS]
  9. init first-admin step (C-4)             <- (2)

Phase 3 (Per-user workspace — config + owner tag)
  10. user_config.py merge (R5/SC-003)       <- (1)   [reads user_config table]
  11. run_pipeline owner= kwarg (R6/SC-005)  <- nothing (pure kwarg)
  12. serve tick: merge + pass owner         <- (10),(11)
  13. web /signup + /signin (session tokens) <- (5),(6)

Polish
  14. docs + standing guards (portability, one-record, knob-docs)
  15. v2->v3 upgrade test + integration owner-isolation test
```

Critical path: **1 → 2 → 3 → 8** (migration → accounts → roles → the guard
that `run`/`schedule` use). Phases 2 and 3 are parallel after Phase 1. Phase 3
item 12 (serve merge) is the only cross-phase join: it needs both the
`user_config` merge (10) and the `owner` kwarg (11).

## Risks

| Risk | Mitigation |
|---|---|
| **Last-admin guard off-by-one** (a demote/delete that races with a concurrent admin action removes the last admin) | The guard counts admins *within the same transaction* as the mutation (`BEGIN` → count → apply → `COMMIT`); the 001 SQLite connection already serializes writers. A red test asserts the refusal for the single-remaining-admin case (SC-002). |
| **Owner tag drift** (a custom source or a future trigger path forgets to pass `owner=`) | `run_pipeline` defaults `owner=None` (legacy/system); the owner is carried by `scheduled_by` already, so a missing tag is detectable by auditing "points whose `owner` is NULL but whose audit `scheduled_by` is a user." The one-record guard (SC-006) catches accidental duplication, not tag drift — add a test that a `run --as alice` run produces points with `owner=alice` (R6). |
| **Per-user merge mutates the shared global config** (alice's cap leaks into bob's run) | The merge returns a *new* dict (`copy.deepcopy` of the relevant source entries) — the 001 `load()` result is never mutated in place. A red test (SC-003) asserts one user's cap change never alters another user's run, in both orderings (alice-then-bob, bob-then-alice). |
| **`auth_checker` breaks 002 callers** | The kwarg defaults to `None`; the `do_GET` wrap is `if self.auth_checker is not None`. A red test asserts 002's no-auth `/status` still returns 200 when no checker is passed (C-5 backward-compat). |
| **Session/token hash cost at the only auth-gated route** | `/status` is the sole HTTP auth gate in 003; a single pbkdf2 verify (~50 ms) is well under the SC-004 500 ms budget. No hot per-item loop touches the token path. (If a high-QPS API is added later, switching tokens to a fast hash is a local change — R2.) |
| **`role` value space** (a legacy `owner` row in a v1/v2 DB) | v3 adds **no** `CHECK` on `role` (data-model.md note) so legacy rows survive; the application layer (`ROLE_CAPS`) treats an unknown role as reader-equivalent (deny mutating) rather than crashing. Documented as a deliberate v1 ceiling. |
| **MCP surface** | A1: MCP runs route through 002's shared pipeline; 003's contract is "same credentials accepted on all four surfaces." The MCP credential plumbing itself is 004's (BR-11.5); 003 ships the identity model + the `auth_checker` the MCP surface will reuse. No MCP code in 003. |

## Testing strategy (Test-First, constitution III)

Every task below has a **red test written before its code** (the tasks.md will
order each red test immediately before its implementation). The automated
checks map to the spec's success criteria:

- **SC-001 (role matrix)**: `tests/unit/test_roles.py` — for every cell of the
  R3 matrix (3 roles × 8 capabilities), a test asserts allow/deny. The guard
  function is the single source; the test iterates the matrix programmatically
  so a new capability row must get a test or the matrix test fails.
- **SC-002 (last-admin guard)**: `tests/unit/test_accounts.py` — demoting the
  last admin, deleting the last admin, both refused with a named error; a
  second admin present → the same action succeeds.
- **SC-003 (per-user config isolation)**: `tests/unit/test_user_config.py` —
  alice sets her hermes `max_items=50`; bob's run uses the global cap. Both
  orderings (and a no-overlap control).
- **SC-004 (four surfaces, same credentials)**: `tests/unit/test_auth_checker.py`
  + a surface test per CLI path (`run --as`, `account`, `token`) + the web
  `/signin` — the same (email, password) authenticates on each; each without
  credentials → 401/403 (exit 2 for the CLI).
- **SC-005 (owner-tagged points)**: `tests/unit/test_owner_tag.py` +
  `tests/integration/test_owner_isolation.py` — a run under alice produces
  points with `owner=alice`; an owner-filtered query returns only alice's.
- **SC-006 (001 guards stay green)**: `tests/integration/test_portability.py`
  (no host paths — the new `docs/` + env-var names are checked) and the
  one-record dedup test re-run; `tests/unit/test_knob_docs.py` (no new knob,
  so unchanged — a guard that 003 added no undocumented knob).
- **v2→v3 upgrade**: `tests/integration/test_multi_user_upgrade.py` — a v2
  DB (accounts + highwater + audit_runs + schedules) upgraded to v3 preserves
  all four tables byte-identical plus the two new `accounts` columns
  defaulted.

The web UI surface (item 13) is the thinnest: `/signup` (create reader) and
`/signin` (return session token) are tested with `http.client` against a
`ThreadingHTTPServer`, asserting the token round-trips through the C-5
`auth_checker`.
