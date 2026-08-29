# Quickstart / Validation Guide: Multi-User Sign-Up, Roles & Per-User Surfaces

Runnable validation scenarios proving the slice works end-to-end. Assumes the
feature is implemented per `plan.md`. The default test suite needs no live
stores: Qdrant runs in-memory and Neo4j/LLM are stubbed (001 pattern).

## Conventions (host-neutrality, NFR-13 / SC-006)

- No host paths, usernames, or interpreter pins in this file. Placeholders
  only: `<admin>`, `<reader>`, `<scheduler>`, `<STATE_DIR>`, `<PORT>`,
  `<PASSWORD>`.
- **State directory**: the 001 `KB_STATE_DIR` env var (or the `state_dir`
  config default). All checks use `$KB_STATE_DIR` — never a literal path.
- **CLI**: `digital-twins` (the console script installed by `pip install .`).
- **Credentials are env vars, never argv** (R9): `DT_USER_PASSWORD` (account
  password, 002), `DT_PERSONAL_TOKEN` (a personal token, 003),
  `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD` (first-admin, 003),
  `DT_SERVICE_TOKEN` (the BR-10 shared service token).
- The state DB is `state.db` inside `$KB_STATE_DIR` (001 `db.DB_FILENAME`).

## Prerequisites

- 001 installed and the package importable (`python -m digital_twins`
  resolves); the state dir migrates to v3 on first command (constitution VI).
- One source enabled for the owner-tag test (e.g. `fs` pointing at a temp dir
  via the 001 `sources.fs.extra.dir` knob).
- Endpoints reachable for the live scenarios: `digital-twins validate` exits
  0. For the in-suite scenarios, Qdrant runs in-memory and the LLM is stubbed.

## Scenario 0 — Fresh install: first account is admin, second is reader (US1, SC-001)

```bash
# 0) a clean state dir for the demo (the 001 state_dir knob, resolved at runtime)
export KB_STATE_DIR="<STATE_DIR>"

# 1) init: endpoints + state db + FIRST admin account (C-4)
#    (interactive: prompts for email + password; or set the two env vars)
export INIT_ADMIN_EMAIL="<admin>"
export INIT_ADMIN_PASSWORD="<admin-pw>"
digital-twins init --yes            # --yes skips the endpoint prompts (already set)
# expect: endpoints reported; "created first admin account <admin>"

# 2) a second account signs up as a reader (C-4; via the CLI signup path)
digital-twins signup --email "<reader>" --password "<reader-pw>"
# expect: "created account <reader> role=reader"

# 3) verify the roles directly from the state db
sqlite3 "$KB_STATE_DIR/state.db" \
  "SELECT email, role FROM accounts ORDER BY id;"
# expect:
#   <admin>|admin
#   <reader>|reader
```

Automated form: `tests/unit/test_accounts.py` — `init` on an empty DB creates
an admin; a second `create_account` is a reader; a re-run of `init` does not
create a second admin.

## Scenario 1 — Sign in on every surface; each denies unauthenticated (US4, SC-004)

```bash
# A) CLI run --once --as <reader> (002's path, now role-checked): a reader may
#    NOT trigger a run (R3: reader = query only) -> exit 2, named reason
export DT_USER_PASSWORD="<reader-pw>"
digital-twins run --once --as "<reader>"
# expect: exit 2, "role 'reader' may not trigger a run"
unset DT_USER_PASSWORD

# B) CLI run --once --as <scheduler> (a scheduler may trigger runs) -> ok
export DT_USER_PASSWORD="<scheduler-pw>"
digital-twins run --once --as "<scheduler>"
# expect: exit 0, audit row scheduled_by=<scheduler>
unset DT_USER_PASSWORD

# C) the /status endpoint, now auth-gated (C-5): no credential -> 401
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:<PORT>/status"
# expect: 401
# with the shared service token -> 200
curl -s -H "Authorization: Bearer $DT_SERVICE_TOKEN" \
  "http://localhost:<PORT>/status" | python -m json.tool
# expect: 200 + the 002 status payload

# D) web UI sign-in returns a session token (R4)
#    (in-suite: tests/.../test_auth_checker.py hits /signin with http.client
#     and asserts the returned token authenticates a subsequent /status)
```

Automated form: `tests/unit/test_auth_checker.py` + the CLI role tests — the
same credentials authenticate on each surface; each without credentials is
denied (401/403, exit 2 for the CLI).

## Scenario 2 — Roles: a scheduler manages schedules, a reader cannot (US2, SC-001)

```bash
# A) a scheduler adds a schedule for a source (scheduler MAY manage schedules)
export DT_USER_PASSWORD="<scheduler-pw>"
digital-twins schedule add --as "<scheduler>" --source fs \
  --preset every-N-hours --param 1
# expect: "schedule id=N next_fire_at=..."
unset DT_USER_PASSWORD

# B) the same scheduler tries to manage accounts (scheduler MAY NOT) -> denied
export DT_USER_PASSWORD="<scheduler-pw>"
digital-twins account list
# expect: exit 2, "role 'scheduler' may not manage accounts"
unset DT_USER_PASSWORD

# C) a reader tries schedule add (reader MAY NOT) -> denied
export DT_USER_PASSWORD="<reader-pw>"
digital-twins schedule add --as "<reader>" --source fs --preset hourly
# expect: exit 2, "role 'reader' may not manage schedules"
unset DT_USER_PASSWORD

# D) the last-admin guard: with one admin, demoting it is refused (SC-002)
export DT_USER_PASSWORD="<admin-pw>"
digital-twins account set-role --email "<admin>" --role reader
# expect: exit 1, "cannot demote the last admin"
unset DT_USER_PASSWORD
```

Automated form: `tests/unit/test_roles.py` (the full R3 matrix, every cell) +
`tests/unit/test_accounts.py` (last-admin guard: demote + delete the last
admin, both refused; a second admin present → allowed).

## Scenario 3 — Per-user config isolation: alice's cap never alters bob's run (US3, SC-003)

```bash
# A) alice overrides her hermes cap to 50 (a scheduler/admin may set their own)
export DT_USER_PASSWORD="<alice-pw>"
digital-twins config set --as "<alice>" --source hermes --key max_items --value 50
# expect: "set user_config <alice> hermes.max_items = 50"
unset DT_USER_PASSWORD

# B) bob runs the same source with the GLOBAL cap (bob has no override)
export DT_USER_PASSWORD="<bob-pw>"
digital-twins run --once --as "<bob>" --source hermes
# expect: bob's run uses the global hermes cap (NOT 50)
unset DT_USER_PASSWORD

# C) alice's run uses HER cap
export DT_USER_PASSWORD="<alice-pw>"
digital-twins run --once --as "<alice>" --source hermes
# expect: alice's run is capped at 50
unset DT_USER_PASSWORD

# (in-suite: tests/unit/test_user_config.py asserts the merged config for
#  alice == global-with-override and for bob == global, in both orderings)
```

Automated form: `tests/unit/test_user_config.py` — set alice's `max_items=50`;
`merge_user_config(alice)` differs from `merge_user_config(bob)` only on that
key; a run under bob is unaffected by alice's row (SC-003, both orderings).

## Scenario 4 — Personal tokens: two tokens, independent, revocable (US3, SC-004)

```bash
# A) alice creates a personal token (self-service; shown once)
export DT_USER_PASSWORD="<alice-pw>"
TOKEN_A=$(digital-twins token create --as "<alice>")
# expect: the token printed once (the DB stores only its hash, R2)

# B) alice creates a second token; both authenticate independently
TOKEN_B=$(digital-twins token create --as "<alice>")
export DT_PERSONAL_TOKEN="$TOKEN_A"
digital-tokens-checker 2>/dev/null || \
  digital-twins account whoami            # authenticates via DT_PERSONAL_TOKEN
# expect: "authenticated as <alice>"
export DT_PERSONAL_TOKEN="$TOKEN_B"
digital-twins account whoami
# expect: "authenticated as <alice>"

# C) revoke TOKEN_A; TOKEN_B still works (US3 S3: revoking one does not affect the other)
export DT_USER_PASSWORD="<alice-pw>"
digital-tokens-checker 2>/dev/null || digital-twins account whoami
# (with TOKEN_A set) -> now 401/exit 2 "token revoked"
export DT_PERSONAL_TOKEN="$TOKEN_B"
digital-tokens-checker 2>/dev/null || digital-twins account whoami
# (with TOKEN_B set) -> still "authenticated as <alice>"
unset DT_PERSONAL_TOKEN
unset DT_USER_PASSWORD
```

Automated form: `tests/unit/test_personal_tokens.py` — create two tokens; verify
each independently; revoke one; the other still verifies; the revoked one
fails.

## Scenario 5 — Owner-tagged points, retrievable by owner filter (US5, SC-005)

```bash
# (in-suite: tests/integration/test_owner_isolation.py)
#   - run under alice (fs source, in-memory Qdrant): points carry owner=alice
#   - run under bob on the same content: the DEDUP invariant holds (one point
#     per fixture, NFR-1) — the owner is the run's scheduled_by, not a second
#     copy
#   - a query with the owner filter (payload owner == alice) returns only
#     alice's points
```

Automated form: `tests/integration/test_owner_isolation.py` — two users' runs,
then a Qdrant `scroll` filtered on `owner` (and on `owner_tag`) returns each
user's content only. The one-record guard (SC-006) is re-run: the same content
ingested by alice and bob yields one point, not two (the owner tag is a
payload field, not a dedup key).

## Scenario 6 — Upgrade safety: v2 state preserved through v3 (SC-006, NFR-15)

```bash
# (in-suite: tests/integration/test_multi_user_upgrade.py)
#   - seed a v2 DB (accounts + highwater + audit_runs + schedules)
#   - run migrate() to v3
#   - assert all four tables byte-identical; accounts gains
#     created_at/last_active defaulted to ''; user_version == 3
```

## Exit criteria

All scenarios pass; `pytest` green; `tests/integration/test_portability.py`
green (no host paths — the new `docs/` + env-var names are checked); the
one-record dedup guard green; `docs/` documents the three roles, the
credential env vars, and the owner-filter query — all host-neutral (SC-006).
