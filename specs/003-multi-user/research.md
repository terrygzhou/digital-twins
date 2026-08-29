# Research: Multi-User Sign-Up, Roles & Per-User Surfaces

Phase 0 output. All Technical-Context unknowns resolved against the 001/002
codebase; no open items. Every decision reuses shipped 001/002 machinery where
it exists (no re-invention) and adds new modules where it does not.

## R1 — Password hashing: reuse 001's `auth.hash_password`, not a new scheme

- **Decision**: Account passwords continue to use 001's
  `digital_twins.auth.hash_password` / `authenticate` —
  `pbkdf2_hmac(sha256, password, salt, 100_000)` with a random 16-byte salt,
  stored as `pbkdf2$<salt_hex>$<hash_hex>` in `accounts.password_hash`. 003 adds
  **no** new password scheme.
- **Rationale**: 001 already ships a stdlib-compatible, salted KDF (A3: "must
  be stdlib-compatible, e.g. PBKDF2 via `hashlib`"). `authenticate(db, owner,
  password)` already does the constant-time compare (`hmac.compare_digest`).
  Re-inventing a second hash path would create two formats to migrate and two
  attack surfaces for one credential type. Constitution I/IV: no new knob, no
  new dependency.
- **Alternatives considered**: bcrypt/argon2 (requires a C extension or a new
  dependency — rejected: stdlib-only is the 001/002 invariant); a re-tuned
  PBKDF2 iteration count (rejected: changing the KDF parameter would break
  001's stored hashes and the v1→v2→v3 upgrade path; 100k sha256 is adequate
  for a local-KB tool).

## R2 — Personal tokens: random 32-byte hex, hashed with the same pbkdf2 scheme

- **Decision**: A personal token is `os.urandom(32).hex()` (64 hex chars,
  256 bits of entropy), shown **once** at creation. The DB stores only
  `pbkdf2$<salt_hex>$<hash_hex>` (the 001 `hash_password` output) of the
  plaintext token in `personal_tokens.token_hash`. Verification
  (`token_matches`) is a single pbkdf2 re-derivation + `hmac.compare_digest`.
- **Rationale**:
  - 32 bytes is the OAuth 2.0 / API-key convention (≥256-bit entropy); a
    password-length token would be guessable by an attacker who learns the
    format. Random hex is unambiguous and copy-paste-safe.
  - **Same hash scheme as passwords** (pbkdf2, 100k): one KDF to reason about,
    one `compare_digest` code path, no new module. The cost is a ~50 ms
    verification per token check — acceptable because the only auth-gated
    surface in 003 is `/status` (C-5) plus the CLI paths, none of which is a
    hot per-item loop. (If a high-QPS API surface is added later, switching
    tokens to a fast SHA-256 of a server-side salt is a local change, not a
    migration.)
  - One-time display: the DB holds only the hash, so a DB leak does not yield
    live credentials (same property as the password column).
- **Alternatives considered**: 64-char alphanumeric (no entropy gain over 32
  bytes, more characters to transcribe); SHA-256 token hash (faster, but adds
  a second KDF format to the same DB and a second code path for no v1 benefit);
  JWT-signed tokens (stateless, but C-1 explicitly wants a revocable,
  server-side store — a JWT cannot be revoked without a denylist, which is
  exactly the table we are adding).

## R3 — Role model: 3 roles, exact capability matrix

Locked by Q3 (do not re-litigate). The enforcement point is C-2: the HTTP/API
route layer (a middleware/guard) for mutating endpoints, plus the CLI's
post-auth role check. The pipeline (`run_pipeline`) is role-agnostic (C-2) —
it trusts the caller to have passed the gate.

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

- **Rationale**:
  - reader = pure query (US2 S1: "mutating routes denied with 403"); the only
    reader capability is reading, so its row is all-✓-read/all-✗-mutate.
  - scheduler = "manage schedules + trigger runs + own run history" (US2
    body); it must NOT reconfigure endpoints, manage accounts, or edit global
    config — so the last three rows are ✗. It *can* manage its **own**
    personal tokens/config (US3: a user's workspace is theirs).
  - admin = everything, plus the cross-user capabilities (all-history,
    all-config, account management).
  - The "own vs all" split is the key axis: a user always manages their own
    workspace; only admin crosses that boundary.
- **Last-admin guard** (SC-002): a role-change or account-delete that would
  leave zero `role='admin'` rows is refused with a named error. This is a
  pre-condition check in the account-management helper, not a schema
  constraint (it depends on counting the surviving admins after the
  hypothetical change).
- **Alternatives considered**: a 4th "owner" role (001's seed used `role='owner'`
  as a test fixture, but Q3 locks exactly three roles; the `owner` value is
  legacy test data, not a shipped role); capability sets as a config knob
  (rejected: Q3's matrix is fixed for v1; a knob would re-open the locked
  decision and add a config surface with no v1 user).

## R4 — Web UI sign-in session: server-side session token in a new table

- **Decision**: The web UI's `/signup` (reader creation) and a new `/signin`
  return a **session token**: `os.urandom(32).hex()`, stored server-side in a
  new `sessions` table (`session_token` UNIQUE, `account_email`, `created_at`,
  `expires_at`, `revoked`). Subsequent UI requests carry the token (a short
  TTL, default 8 h, re-resolved per request against `sessions`); the same
  middleware that gates the API gates the UI (C-2). The shared service token
  (BR-10) and personal tokens are *also* accepted by that middleware — a
  session token is just one of three credential types, all checked by the same
  `auth_checker` (C-5).
- **Rationale**: C-2 offers "short-lived session token, server-side store or
  signed cookie." A **server-side store** wins over a signed cookie because:
  (a) C-1/C-2 want revocable, server-side credential state (a signed cookie
    cannot be revoked without the same denylist the store already is);
  (b) one `sessions` table mirrors `personal_tokens` exactly, so the
  middleware has a single "look up a hash/tokens table" shape for both;
  (c) the UI is minimal in v1 (A2) — a 32-byte token in a cookie is the
  minimum that satisfies "subsequent requests carry that token." The token is
  **not** a long-lived API key: it expires (`expires_at`) and is revocable,
  which a static cookie/signed-JWT would not be.
- **Alternatives considered**: signed (HMAC) session cookie (stateless, but
  un-revocable and leaks the signing key's scope on every request — worse for
  a multi-user surface); raw password re-sent per request (rejected: the
  browser would store plaintext credentials, violating the "never in
  argv/logs/long-lived storage" principle); per-surface credential separation
  (rejected: US4 S1 explicitly wants the *same* credentials accepted on all
  four surfaces — one identity model).

## R5 — Per-user config merge: which keys, exact algorithm

- **Decision**: Per-user overrides are stored in `user_config`
  (`account_email`, `source`, `key`, `value` TEXT, `updated_at`,
  UNIQUE(account_email, source, key)) — a flat key-value store (C-3).
  **Overridable keys**: the per-source knobs that a user might plausibly tune
  for "my KB": `enabled`, `max_items`, `timeout_s`. (These are the only
  per-source knobs a user would set; `prefix`/`entrypoint`/`credential`/`email`
  are either source-identity or global-secret knobs and are NOT user-overridable
  in v1.)
- **Merge algorithm** (pure, at the caller — serve tick or `run --as`):
  1. Start from the resolved global config dict (001's four-layer `load()`
     output).
  2. For the run's owner `U` and each source `S` that `U` has a `user_config`
     row for, parse `value` into the knob's declared type (001
     `schema.coerce("sources.<S>.<key>", value)` — fail-fast on a malformed
     value, constitution IV) and set it on `cfg["sources"][S][key]`.
  3. The resulting dict is what `run_pipeline` receives. `run_pipeline`'s
     signature is **unchanged** (C-3): it is a pure function of its arguments,
     and the merge happens in the caller so two users' runs never share a
     mutated global.
- **Precedence**: `user_config(U, S, key)` > global config resolution
  (C-3). Within the global config, 001's precedence (env > kb.local.yml >
  kb.yml > defaults) is untouched; the user override is the *highest* layer,
  applied after `load()` returns.
- **Rationale**: A flat KV store (not a nested YAML blob) because:
  (a) C-3 specifies `UNIQUE(account_email, source, key)` — the schema *is* the
     KV shape; (b) one row per knob means a user can override `max_items` on
     `hermes` without touching `timeout_s`; (c) the value is TEXT and coerced
     at merge time, so the store is type-agnostic and the 001 `coerce` is the
     single type validator (no second validator to keep in sync).
- **Alternatives considered**: a per-user YAML file (rejected: a 5th config
  layer outside 001's documented four, and a file on disk per user is a
  host-path leak — NFR-13); a nested `user_config` JSON column (rejected: C-3's
  `UNIQUE(account_email, source, key)` demands the flat shape); overriding
  *all* knobs including endpoints (rejected: endpoint keys are global/secret;
  letting a reader point qdrant elsewhere is a capability, not a knob).

## R6 — Owner-tag stamping: where in the pipeline

- **Decision**: The owner tag is stamped into the **Qdrant point payload** at
  the same place 001 already builds the payload (the `qm.PointStruct(payload={...})`
  in `run_pipeline`). Concretely: `run_pipeline` gains an optional
  `owner: str | None = None` parameter (default `None` = legacy/system). When
  set, each point's payload gains `"owner": owner` and
  `"owner_tag": f"{owner}-ingest"` (the spec's Q2 owner-filter field). The
  `audit_runs` row already carries `scheduled_by` (= the owner) — no audit
  schema change; the owner tag is the *query-time* filter, the audit row is
  the *history-time* attribution.
- **Rationale**:
  - Stamping in `run_pipeline` (not the source) keeps the tag independent of
    the source's own read logic — every source, including custom ones, gets the
    same owner stamp without per-source changes.
  - Adding a *parameter* to `run_pipeline` (not a new module) is the minimum
    that satisfies "per-user owner tags on ingested points" while keeping the
    pipeline a pure function (constitution II): the owner is just another input,
    exactly like `scheduled_by` already is. The C-3 "signature unchanged"
    guarantee refers to the *config* argument (the merge happens at the
    caller); an additive `owner` kwarg does not break 001/002 call sites
    (default `None`).
  - `owner_tag` (a `"<user>-ingest"` string) is a second payload field
    alongside `owner` so a user can filter by *either* the raw email or the
    spec's documented tag form — US5 S1 names both (`owner == <user>` /
    `tag == <user>-ingest`).
- **Alternatives considered**: a Neo4j-only owner (rejected: Qdrant is the
  primary query surface; the owner must live on the point payload for
  post-filtering); a separate `owner` column on `audit_runs` (rejected:
  `scheduled_by` already carries it — a duplicate would diverge); stamping in
  each source's `read()` (rejected: N source edits for one cross-cutting
  tag, and a custom source could forget it).

## R7 — `init` first-admin flow (C-4): where the prompt lives

- **Decision**: `digital-twins init` gains a first-account step *after* the
  state DB is migrated (so the `accounts` table exists). If the `accounts`
  table is **empty**, `init` prompts for an email + password (or reads
  `INIT_ADMIN_EMAIL` / `INIT_ADMIN_PASSWORD` env vars, the 002 pattern of
  auth-only env vars) and inserts the first account with `role='admin'`. If
  the table is **non-empty**, `init` does NOT create an account (the first
  admin already exists) — it just confirms "admin already exists:
  <email>" and skips. The web UI's `/signup` (new endpoint) calls the *same*
  shared helper `create_account(db, email, password, role)` where `role` is
  resolved by the rule "first row in accounts? → admin, else reader" (C-4).
- **Rationale**:
  - C-4 explicitly says "both paths share the same account-creation helper" —
    so the role-determination logic lives in ONE function
    (`create_account` / `next_role_for`), not duplicated in `init` and
    `/signup`.
  - Reading `INIT_ADMIN_EMAIL`/`INIT_ADMIN_PASSWORD` from env (auth-only, not a
    config knob) mirrors 002's `DT_USER_PASSWORD` pattern (R-06/R-05 in 002)
    and keeps `init` non-interactive in host-cron/CI contexts while still
    prompting interactively in a terminal.
  - "First row → admin" is evaluated *at insert time* against the current
    table: on a fresh install the table is empty → admin; on a re-run of
    `init` it is not → skip. This makes `init` idempotent (constitution IV).
- **Alternatives considered**: a separate `create-admin` command (rejected:
  C-4 says `init` creates the first admin; a second command would fragment the
  "first run" story); hardcoding `role='admin'` in `init` without checking
  emptiness (rejected: a re-run of `init` would create a *second* admin row —
  the "first row" guard prevents that); making `/signup` also create admins
  (rejected: C-4/US1 S2 say subsequent sign-ups are readers by default).

## R8 — `/status` auth gating (C-5): the `auth_checker` contract

- **Decision**: `StatusServer.__init__` gains an optional `auth_checker`
  callable (C-5). Signature: `auth_checker(headers: dict[str, str]) ->
  bool | str`. Return semantics:
  - `True` → allow (200).
  - a `str` → deny with that string as the body's `error` (403 if the
    credential is *known but insufficient*, 401 if *missing/unknown*).
  - `False` → deny with a generic "unauthorized" (401).

  The serve CLI passes a checker that accepts, in order:
  1. the shared service token (BR-10) — `Authorization: Bearer <service>`
     compared (constant-time) against the configured `DT_SERVICE_TOKEN` env
     var (auth-only, not a config knob);
  2. a valid personal token — `Authorization: Bearer <personal>` looked up in
     `personal_tokens` (hash match, not revoked, account exists);
  3. a valid session token — `Authorization: Bearer <session>` looked up in
     `sessions` (not expired, not revoked).

  Requests without a valid credential → 401. A credential that resolves to a
  reader calling a *mutating* endpoint → 403 (there are no mutating HTTP
  endpoints in 003's `/status` — it is read-only — so in practice the 403
  path is exercised by the account/config routes when they exist; in 003 the
  `auth_checker` only needs to distinguish "known credential" from "unknown,"
  and the 403 branch is reserved for a future mutating route).

  **Backward compatibility**: `StatusServer` with no `auth_checker` (002's
  callers) behaves exactly as 002 (no auth) — the `do_GET` wraps the check
  *only when* `auth_checker` is not None (C-5).
- **Rationale**:
  - A *callable* (not a table of credentials) keeps `status.py` decoupled from
    the credential store: the serve CLI owns the lookup logic (it has the DB
    connection), and `status.py` only forwards the request headers. This is
    the minimum surface that satisfies C-5 and is testable with a stub checker.
  - The `bool | str` return (rather than raising) lets the handler map the
    result to the right HTTP code without exception plumbing in the hot path.
- **Alternatives considered**: a decorator on `do_GET` (rejected: the handler
  is a single `do_GET`; a middleware stack is over-weight for one route);
  rejecting at the socket level (rejected: the 401/403 distinction requires
  *reading* the credential first, which happens after the request is
  received); a separate auth table for `/status` only (rejected: C-1 says
  personal tokens / sessions are the credential store, shared with the API).

## R9 — CLI credential transport for the new commands

- **Decision**: 003's new/changed CLI commands authenticate the same way 002's
  `run --once --as` does (password via `DT_USER_PASSWORD` env, or a personal
  token via `DT_PERSONAL_TOKEN` env — added in 003), never in argv, never in
  logs. The `account` and `token` subcommands (admin/self-service) resolve the
  caller's identity first (password or personal token), then check the caller's
  role against the R3 matrix before the mutation.
- **Rationale**:
  - `DT_PERSONAL_TOKEN` is the non-interactive credential for admin/scheduler
    operators (host-cron, CI): they hold a personal token, not a password in
    an env var that could be guessed. It is read directly from
    `os.environ` (auth-only, not a config knob — same as 002's
    `DT_USER_PASSWORD`).
  - The caller's role is checked **post-auth** (C-2): authentication
    ("who are you") always runs before authorization ("may you do this"). A
    reader running `digital-twins account list` authenticates fine but is then
    denied (403 / exit 2 with a named "role insufficient" reason).
- **Alternatives considered**: a `--token` argv flag (rejected: visible in
  `ps`; the env-var pattern is 002's established one); a credential file
  (rejected: a host-path artifact outside the config layer — NFR-13); the
  password prompting interactively for *every* admin command (rejected: breaks
  host-cron; the env-var + personal-token combo covers both interactive and
  automated).
