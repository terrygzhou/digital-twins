# Multi-User: Roles, Credentials, and Per-User Surfaces

This page documents the 003 multi-user surface: the three roles and their
capability matrix, the credential environment variables, the owner-filter
query, the first-admin / sign-up flow, and the per-user config merge
precedence. All values resolve through the config layer at runtime; nothing
here is a host-specific constant. No host paths, no interpreter pins, no
literal usernames — every example uses a placeholder.

## The three roles

003 legalizes exactly three roles (Q3, locked — do not re-litigate):

- **`admin`** — every capability, including the cross-user ones: view all
  users' run history, manage all users' config, and account management
  (create / demote / delete / role).
- **`scheduler`** — manage schedules + trigger runs + own run history. Must
  **not** reconfigure endpoints, manage accounts, or edit global config. It
  *can* manage its **own** personal tokens and config (a user's workspace is
  theirs).
- **`reader`** — pure query. Mutating routes are denied with 403. Its only
  capabilities are reading: sign in, query `/status`, view its own run
  history.

Role is a **data value** on `accounts.role`, not a schema enum (the v3
migration is additive-only; 001/002 legacy rows may carry the test `owner`
value, which is not a shipped role). The *application* layer enforces the
three-role set.

### R3 capability matrix

The enforcement point is the HTTP/API route layer (a middleware/guard) for
mutating endpoints, plus the CLI's post-auth role check. The pipeline
(`run_pipeline`) is role-agnostic — it trusts the caller to have passed the
gate.

| Capability | admin | scheduler | reader |
|---|:---:|:---:|:---:|
| Sign in (any surface) | ✓ | ✓ | ✓ |
| Query `/status` (read) | ✓ | ✓ | ✓ |
| View own run history | ✓ | ✓ | ✓ |
| View all users' run history | ✓ | ✗ | ✗ |
| Trigger a run (`run --once`) | ✓ | ✓ | ✗ |
| Schedule CRUD (`schedule add/list/remove`) | ✓ | ✓ | ✗ |
| Manage personal config overrides (own) | ✓ | ✓ | ✗ |
| Manage personal tokens (own) | ✓ | ✓ | ✗ |
| Endpoint / global config write | ✓ | ✗ | ✗ |
| Account management (create/demote/delete/role) | ✓ | ✗ | ✗ |
| Manage **all** users' config | ✓ | ✗ | ✗ |

The "own vs all" split is the key axis: a user always manages their own
workspace; only `admin` crosses that boundary.

### Last-admin guard

A role-change or account-delete that would leave zero `role='admin'` rows is
refused with a named error. This is a pre-condition check in the
account-management helper, not a schema constraint (it depends on counting the
surviving admins after the hypothetical change).

## Credential environment variables

All four are **auth-only**: they are not config knobs, do not participate in
the four-layer config precedence, and are read directly from the environment.
None is ever placed in argv (so it does not appear in `ps` output or shell
history) and none is written to logs. No literal username ships in this
document.

| Variable | Purpose | Read by |
|---|---|---|
| `DT_USER_PASSWORD` | The account's password. Now read by **every** mutating command (`run --once --as`, `schedule`, `account`, `token`), not just `run --once`. | All 003 mutating CLI commands |
| `DT_PERSONAL_TOKEN` | A personal token. When set, a command authenticates with the token *instead of* the password (the token resolves to an account + role). A personal token and a password are alternatives; the CLI uses the token when this is set, else the password. | All 003 mutating CLI commands |
| `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD` | The first-admin's email + password. Read by `init` when creating the first account (C-4). | `init` (first-admin step) |
| `DT_SERVICE_TOKEN` | The BR-10 shared service token. Read by the `serve`-supplied `auth_checker` to gate `/status` (C-5/R8). | `serve` (status auth gate) |

Personal tokens are random 32-byte hex (256-bit entropy), shown **once** at
creation. The DB stores only the pbkdf2 hash of the plaintext token; a DB
leak does not yield live credentials. A user may hold multiple active tokens
(browser session + API key + MCP token); revoking one does not affect the
others.

## Owner-filter query

All users share the same KB store (single Qdrant collection + Neo4j graph).
Per-user tags are stamped on ingested points so a user may query "only what I
ingested." `run_pipeline` gains an optional `owner` parameter; when set, each
point's payload gains:

- `payload["owner"] = <user>` — the raw account email, and
- `payload["owner_tag"] = "<user>-ingest"` — the spec's documented tag form.

A run with `owner=None` (legacy / system) stamps no owner field (002 behavior
preserved). The owner tag is a **payload field, not a dedup key**: the same
content ingested by two users still yields one point (001's deterministic
point IDs + high-water marks are untouched). The tag is for *query-time*
filtering, not for dedup.

To query "only what I ingested," post-filter on **either** field:

```python
# Filter to <user>'s content:
where = f"owner == '{user}'"          # payload.owner == <user>
# or equivalently:
where = f"owner_tag == '{user}-ingest'"  # payload.owner_tag == <user>-ingest
```

Both forms refer to the same set of points; pick whichever the caller finds
clearer. The run's audit row already carries `scheduled_by = <user>` — the
audit row is the *history-time* attribution, the owner tag is the *query-time*
filter.

## First-admin / sign-up flow

Account creation is shared between `init` and the web UI's `/signup` (C-4) —
both call the same `create_account` helper, and the role is resolved by one
rule: **"first row in `accounts`? → admin, else reader."**

### `init` (first-admin step)

`digital-twins init` gains a first-account step *after* the state DB is
migrated (so the `accounts` table exists):

- If the `accounts` table is **empty**: prompt for an email + password (or
  read `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD`) and insert the first
  account with `role='admin'`. Echo "created first admin account
  `<email>`".
- If the `accounts` table is **non-empty**: do **not** create an account —
  the first admin already exists. Echo "admin already exists: `<email>`" and
  skip.

This makes `init` idempotent: a re-run never creates a second admin.

### `signup` (subsequent accounts)

`digital-twins signup --email <email> --password <password>` (and the web
UI's `POST /signup`) create subsequent accounts. On a fresh install the first
`signup` is an admin; after that, every `signup` is a reader. Duplicate email
→ fail-fast with a clear error ("account already exists: `<email>`"), no
second row. The password is hashed with 001's `hash_password` (pbkdf2,
100k iterations of sha256, random 16-byte salt).

### Web-UI sign-in

The web UI's `POST /signin` authenticates via the password and returns a
**session token** (32-byte random hex): its pbkdf2 hash is stored server-side
in `sessions`, and the plaintext is returned to the UI once. The session is
**short-lived** (default 8 h) and **revocable**. Subsequent UI requests carry
the session token via `Authorization: Bearer <session>`; the same
`auth_checker` that gates `serve`'s `/status` gates the UI's `/status` — one
identity model, one guard.

All four sign-in surfaces (web UI, HTTP API, MCP, CLI) accept the same
credentials; each without a valid credential is denied (401/403, exit 2 for
the CLI).

## Per-user config merge precedence

Per-user overrides are stored in `user_config` — a flat key-value store
(`account_email`, `source`, `key`, `value`, `updated_at`,
`UNIQUE(account_email, source, key)`). Only the per-source knobs a user would
plausibly tune are overridable: `enabled`, `max_items`, `timeout_s`. The
source-identity and global-secret knobs (`prefix`, `entrypoint`,
`credential`, `email`) are **not** user-overridable in v1.

The merge happens **at the caller** (serve tick or `run --as`), never inside
`run_pipeline` (whose signature is unchanged — the pipeline is a pure
function of its arguments). The algorithm:

1. Start from the resolved global config dict (001's four-layer `load()`
   output).
2. For the run's owner `<user>` and each source `<S>` the user has a
   `user_config` row for, parse `value` into the knob's declared type
   (001 `schema.coerce` — fail-fast on a malformed value) and set it on
   `cfg["sources"][S][key]`.
3. The resulting dict is what `run_pipeline` receives. The global config is
   never mutated, so two users' runs never share a mutated global.

**Precedence** (highest to lowest):

```
user_config(<user>, <S>, key)        # per-user override (003, highest)
global config resolution:
    env (incl. .env)                  # 001 layer 1
    kb.local.yml                      # 001 layer 2
    kb.yml                            # 001 layer 3
    built-in defaults                 # 001 layer 4
```

The user override is the *highest* layer, applied after `load()` returns.
Within the global config, 001's precedence (env > kb.local.yml > kb.yml >
defaults) is untouched.

## See also

- `specs/003-multi-user/spec.md` — full user stories and success criteria.
- `specs/003-multi-user/contracts/cli.md` — CLI contract (003 additions).
- `specs/003-multi-user/contracts/scheduler.md` — scheduler & status contract.
- `specs/003-multi-user/data-model.md` — the three new tables and v3 migration.
- `specs/003-multi-user/research.md` — R1–R9 (role matrix, tokens, merge).
- `docs/scheduling.md` — the 002 scheduling surface.
