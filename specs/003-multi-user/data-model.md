# Data Model: Multi-User Sign-Up, Roles & Per-User Surfaces

All tables live in the 001 state store (`state.db`, `PRAGMA user_version=3`
after this slice). 003 adds **three new tables** and adds **two columns** to
`accounts`. The v1/v2 tables (`accounts` base, `highwater`, `audit_runs`,
`schedules`) are **not altered** beyond the two additive `accounts` columns —
the migration is purely additive (constitution VI, NFR-15).

## Migration v3 (additive)

One migration step, `apply_v3`, runs after v2 (the `migrations.MIGRATIONS`
list gains `(3, apply_v3)`; `SCHEMA_VERSION` becomes 3). The step is
transactional like 001/002: `BEGIN` → DDL → `PRAGMA user_version=3` →
`COMMIT`; a failure rolls back and the version does not advance.

DDL:

```sql
-- 1. accounts: two additive columns (001/002 rows gain defaults; no data
--    loss, no rewrite).
ALTER TABLE accounts ADD COLUMN created_at   TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN last_active  TEXT NOT NULL DEFAULT '';

-- 2. personal_tokens (C-1: separate from accounts; multi-token; revocation
--    isolation).
CREATE TABLE IF NOT EXISTS personal_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_email TEXT NOT NULL,              -- logical FK to accounts.email
    token_hash    TEXT NOT NULL UNIQUE,       -- pbkdf2$salt_hex$hash_hex (R2)
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (account_email) REFERENCES accounts(email) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_personal_tokens_account
    ON personal_tokens (account_email);

-- 3. user_config (C-3: per-user, per-source, per-key overrides).
CREATE TABLE IF NOT EXISTS user_config (
    account_email TEXT NOT NULL,
    source        TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,              -- type-coerced at merge (R5)
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (account_email, source, key)
);

-- 4. sessions (R4: web-UI sign-in, server-side session store).
CREATE TABLE IF NOT EXISTS sessions (
    session_token TEXT PRIMARY KEY,            -- pbkdf2$ hash of the token (R4)
    account_email TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (account_email) REFERENCES accounts(email) ON DELETE CASCADE
);
```

> **Note on `accounts` column defaults.** The two new columns are
> `NOT NULL DEFAULT ''` (empty ISO-8601 sentinel) rather than `NULL` so a
> legacy 001/002 row (pre-v3, no timestamp) is still a valid row and the
> `last_active` ordering query (`ORDER BY last_active DESC`) does not have to
> treat `NULL` specially. A fresh account (created by 003's `init` / `/signup`
> / `create_account`) gets real UTC ISO-8601 values; a migrated legacy row
> keeps `''` until its next sign-in (which sets `last_active`). This is the
> minimum that keeps the v1→v3 upgrade a pure no-op on existing rows.

## Entities

### Account (001 row, extended)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | 001 |
| `email` | TEXT NOT NULL UNIQUE | username (Q8: email-as-username) |
| `role` | TEXT NOT NULL DEFAULT 'reader' | 001; 003 legalizes `admin` \| `scheduler` \| `reader` (Q3). Legacy `owner` test value is not a shipped role (R3). |
| `password_hash` | TEXT | 001; `pbkdf2$salt_hex$hash_hex` (R1) |
| `created_at` | TEXT NOT NULL DEFAULT '' | **NEW (v3)** — account creation, UTC ISO-8601 |
| `last_active` | TEXT NOT NULL DEFAULT '' | **NEW (v3)** — last successful sign-in, UTC ISO-8601 |

Role is a **data value**, not a schema enum: `CHECK` on `role` is deliberately
not added in v3 so that 001/002 legacy rows (which may carry the test `owner`
value) still satisfy the schema after upgrade; the *application* layer
(`next_role_for` / `ROLE_CAPS`) enforces the three-role set. This keeps the
migration additive-only.

### PersonalToken (C-1)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `account_email` | TEXT NOT NULL | FK → `accounts.email`, `ON DELETE CASCADE` |
| `token_hash` | TEXT NOT NULL UNIQUE | `pbkdf2$...` of the plaintext token (R2). The plaintext is shown once at creation and never stored. |
| `created_at` | TEXT NOT NULL | UTC ISO-8601 |
| `last_used_at` | TEXT | set on a successful verify; NULL until first use |
| `revoked` | INTEGER NOT NULL DEFAULT 0 | 0 = active, 1 = revoked. Revoking one token flips *only this row* (US3 S3: "revoking one does not affect the other"). |

Indexes: `token_hash UNIQUE` (the verify path is a single indexed lookup);
`idx_personal_tokens_account` (list/revocation by owner).

### UserConfig (C-3)

| Column | Type | Notes |
|---|---|---|
| `account_email` | TEXT NOT NULL | PK part 1 |
| `source` | TEXT NOT NULL | PK part 2; a source name from the config layer |
| `key` | TEXT NOT NULL | PK part 3; one of `enabled` \| `max_items` \| `timeout_s` (R5) |
| `value` | TEXT NOT NULL | stored as text; coerced via 001 `schema.coerce` at merge time (R5) |
| `updated_at` | TEXT NOT NULL | UTC ISO-8601 |

`PRIMARY KEY (account_email, source, key)` — the C-3 `UNIQUE` constraint. One
row per (user, source, knob). A user may override any subset; absent keys fall
through to the global config (R5 precedence).

### Session (R4)

| Column | Type | Notes |
|---|---|---|
| `session_token` | TEXT PRIMARY KEY | `pbkdf2$...` hash of the 32-byte session token (R4). Plaintext shown to the UI once. |
| `account_email` | TEXT NOT NULL | FK → `accounts.email`, `ON DELETE CASCADE` |
| `created_at` | TEXT NOT NULL | |
| `expires_at` | TEXT NOT NULL | default 8 h after `created_at` (a constant, not a knob — the UI is minimal, A2) |
| `revoked` | INTEGER NOT NULL DEFAULT 0 | explicit sign-out flips this row |

A session is **short-lived and revocable** (unlike a personal token): the
`auth_checker` (R8) rejects it once `expires_at < now` or `revoked = 1`.

### AuditRun (001 shape, no schema change)

001's `audit_runs` already carries `scheduled_by` (the 002 slice legalized the
full `trigger` set and `scheduled_by` values). 003 adds **no column**: the
owner is the `scheduled_by` value, and "my run history" is the query
`WHERE scheduled_by = <user>` (US3 S2). `system`-attributed runs are visible to
admins (all) and to no one's "mine" filter (a system run is not a user's) —
the sharing default (US5 S2) is: system runs are queryable by admins; a
regular user's "mine" view shows only their own `scheduled_by` rows.

## State interactions

- **Sign-in (any surface)** reads: `accounts` (password) **or**
  `personal_tokens` **or** `sessions` (the three credential types, R8). A
  success sets `accounts.last_active` (and `personal_tokens.last_used_at` /
  `sessions` usage as applicable).
- **Account creation** (`init` / `/signup` / `account create`) writes: one
  `accounts` row (+ `created_at`/`last_active`), role resolved by
  "first row → admin, else reader" (R7).
- **Role / account management** (admin) reads: `accounts` (count admins for
  the last-admin guard), writes: `accounts.role` / deletes the row. The
  guard refuses any change that would leave zero admins (SC-002, R3).
- **Per-user config** (`user_config`) is read by the **caller** (serve tick /
  `run --as`) at merge time (R5); the pipeline never reads it directly.
- **Token / session lifecycle** (`personal_tokens`, `sessions`): create
  (show-once plaintext), verify (hash lookup), revoke (flip `revoked`),
  expire (session TTL).
- **Owner tag** (R6): stamped into the Qdrant point payload by
  `run_pipeline(owner=...)` — no new table; the `audit_runs.scheduled_by`
  already records the same owner for history.

## Backward compatibility (v1/v2 → v3)

- **v1 → v3**: `apply_v3` adds the two `accounts` columns with defaults and
  creates the three new tables. Existing `accounts` rows gain `''` for the new
  columns; no existing column is dropped or re-typed. `highwater`,
  `audit_runs` are untouched.
- **v2 → v3**: `schedules` (the v2 table) is untouched; the v3 step only adds
  to `accounts` and creates `personal_tokens` / `user_config` / `sessions`.
- **Fresh install**: `migrate()` runs v1 → v2 → v3 in order (the
  `migrations.MIGRATIONS` list); the resulting `user_version` is 3.
- **Data preservation test** (mirrors 001/002's `test_upgrade.py`): a
  v2-seeded DB (accounts + highwater + audit_runs + schedules) upgraded to v3
  must have all four tables byte-identical plus the two new `accounts`
  columns defaulted — no row lost, no value changed.
- **`authenticate()` unchanged**: 001's `auth.authenticate(db, owner,
  password)` reads only `accounts.email` / `accounts.password_hash` — the two
  new columns do not affect it, so 002's `run --once --as` keeps working
  verbatim on a v3 DB.
