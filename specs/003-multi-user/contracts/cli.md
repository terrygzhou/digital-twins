# Contract: CLI (003 additions)

Extends the 001/002 `click` CLI. 001/002 commands keep working unchanged; 003
adds role enforcement to the mutating paths and three new groups
(`signup`, `account`, `token`). Every 003 path authenticates **first**
(password or personal token), then checks the caller's role against the R3
matrix **before** the mutation (C-2 post-auth guard).

## Credential env vars (003)

- `DT_USER_PASSWORD` — 002's account-password var, now read by **every**
  mutating command (not just `run --once`): `run --once --as`, `schedule`,
  `account`, `token`. Auth-only: not a config knob, read directly from
  `os.environ`, never in argv, never in logs.
- `DT_PERSONAL_TOKEN` — 003's personal-token var. When set, a command
  authenticates with the token *instead of* the password (the token resolves
  to an account + role). Same auth-only / never-in-argv / never-in-logs rules.
  A personal token and a password are alternatives; the CLI uses the token
  when `DT_PERSONAL_TOKEN` is set, else the password.
- `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD` — 003's first-admin vars, read by
  `init` when creating the first account (C-4). Auth-only.
- `DT_SERVICE_TOKEN` — the BR-10 shared service token, read by the
  `serve`-supplied `auth_checker` to gate `/status` (C-5/R8). Auth-only.

## `digital-tokens` — the 003 CLI surface

### `digital-tokens init [--yes]` (003: + first-admin step, C-4)

001/002 behavior unchanged (endpoints, starter `kb.local.yml`, state DB,
health report). 003 adds, **after** the state DB is migrated:

- If the `accounts` table is **empty**: prompt for an email + password (or
  read `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD`) and create the first
  account with `role='admin'`. Echo "created first admin account
  `<email>`".
- If the `accounts` table is **non-empty**: do **not** create an account;
  echo "admin already exists: `<first-email>`" and skip.

This makes `init` idempotent: re-running it never creates a second admin.
Exit codes as 001 (health-report dependent).

### `digital-tokens run --once --as USER` (002's command, 003 role-checked)

002's one-shot path is unchanged except for the 003 post-auth role check:
after `authenticate(db, USER, DT_USER_PASSWORD)` (or the personal-token
resolve) succeeds, the caller's role must permit **trigger a run** (R3:
admin/scheduler yes, reader no). On denial: exit 2, stderr names the role
insufficiency ("role `reader` may not trigger a run"), **no** audit row, **no**
Qdrant write, **no** high-water advance — the same fail-fast contract as 002's
bad-password path. On success: `scheduled_by = USER` (002), and the run's
points carry `owner = USER` (R6).

### `digital-tokens schedule add|list|remove` (002's commands, 003 role-checked)

002's CRUD is unchanged except for the 003 post-auth role check: the caller's
role must permit **schedule CRUD** (R3: admin/scheduler yes, reader no).
`schedule add` additionally validates that the operator may own a schedule for
the named source (a reader's `schedule add` is denied before any row is
written). On denial: exit 2, named reason, no state write.

### `digital-tokens signup --email E --password P` (new, C-4)

Create an account. Role is resolved by the shared helper: "first row in
`accounts`? → admin, else reader" (C-4/R7). On a fresh install the first
`signup` is an admin; after that, every `signup` is a reader. Duplicate email
→ fail-fast (exit 2, "account already exists: `<email>`"), no second row. The
password is hashed with 001's `hash_password` (R1). `created_at` is set to now;
`last_active` to now.

### `digital-tokens account ...` (new, admin-gated except `whoami`)

- `account list` — **(admin)** list accounts: `email`, `role`, `created_at`,
  `last_active`.
- `account set-role --email E --role {admin|scheduler|reader}` — **(admin)**
  change a role. **Last-admin guard** (SC-002): if the change would leave zero
  admins, refuse (exit 1, "cannot demote the last admin"). Otherwise apply and
  record the change in the audit trail (constitution V).
- `account delete --email E` — **(admin)** delete an account. Same
  last-admin guard. Cascades to `personal_tokens` / `sessions` (FK
  `ON DELETE CASCADE`).
- `account whoami` — **(authenticated)** print the caller's identity + role
  (quickstart / debugging). The only non-admin-gated `account` subcommand.

### `digital-tokens token ...` (new, self-service or admin)

- `token create [--as USER]` — create a personal token. Without `--as`: the
  caller's own token (self-service; any authenticated role). With `--as
  USER`: admin-only, create for another user. The plaintext token (32-byte
  random hex, R2) is **printed once**; the DB stores only its pbkdf2 hash.
  `created_at` set; `last_used_at` NULL; `revoked = 0`.
- `token list [--as USER]` — list tokens (self: own; admin: any user's):
  `id`, `created_at`, `last_used_at`, `revoked`. The plaintext is never
  re-displayed (only its hash is stored).
- `token revoke --id N` — revoke token id N. Self: own tokens only; admin: any.
  Flips `revoked = 1` on that row only (US3 S3: revoking one does not affect
  the others).

### Role matrix (R3 — what the CLI enforces)

| Capability | admin | scheduler | reader |
|---|:---:|:---:|:---:|
| Trigger a run (`run --once`) | ✓ | ✓ | ✗ |
| Schedule CRUD | ✓ | ✓ | ✗ |
| Manage own personal config | ✓ | ✓ | ✗ |
| Manage own personal tokens | ✓ | ✓ | ✗ |
| Endpoint / global config write | ✓ | ✗ | ✗ |
| Account management (list/set-role/delete) | ✓ | ✗ | ✗ |
| View all users' run history | ✓ | ✗ | ✗ |
| View own run history | ✓ | ✓ | ✓ |

The CLI enforces this **post-auth** (C-2): authenticate (password or personal
token) → resolve the caller's role → check the matrix → on denial, exit 2 with
a named reason (never the password/token). The mutation does **not** happen on
denial.

## Exit codes

- `0` — success.
- `1` — operational failure (e.g. the schedule id does not exist, 002; the
  last-admin guard refusal is also `1` — it is a *valid admin action that the
  invariant forbids*, not an auth failure).
- `2` — auth/config/role failure: bad/missing credentials, role insufficient,
  fail-fast prerequisite. A role denial and a credential failure are both
  exit 2 but with *distinct* stderr messages (the 002 "no state db" vs
  "authentication failed" split is preserved; 003 adds "role `X` may not
  <capability>").

## Per-user config command (R5)

The per-user config overrides (data-model's `user_config` table) are managed
through the same authenticated surface. The contract:

- `digital-tokens config set --as USER --source S --key K --value V` — set a
  per-user override. The role must permit **manage own personal config**
  (admin/scheduler/reader for *their own* user; admin for another user's).
  `K` is restricted to the overridable keys (`enabled`, `max_items`,
  `timeout_s` — R5); other keys → exit 2 ("not a user-overridable knob").
  `V` is type-coerced via 001 `schema.coerce` at write time (fail-fast on a
  malformed value).
- `digital-tokens config list --as USER` — list a user's overrides.
- `digital-tokens config unset --as USER --source S --key K` — remove one.

(The merge of these overrides into a run's config happens at the caller —
serve tick / `run --as` — per C-3; these commands only manage the table.)

## Documentation obligation (NFR-13 / SC-006)

`docs/` gains a multi-user section: the three roles + the R3 matrix, the four
credential env vars (all host-neutral, no literal usernames), the owner-filter
query, and the first-admin / sign-up flow. The doc must pass the 001
portability guard (no host paths, no interpreter pins, no literal
`/home/<user>` or `python3.12`).
