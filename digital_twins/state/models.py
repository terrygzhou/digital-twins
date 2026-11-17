"""State tables: accounts, highwater, audit_runs, schedules (v1/v2) and the
v3 multi-user tables (personal_tokens, user_config, sessions).

See specs/003-multi-user/data-model.md for the exact DDL. v3 is purely
additive: it creates three new tables and adds two columns to `accounts`
(`created_at`/`last_active`, `NOT NULL DEFAULT ''`) without touching the
001/002 tables (constitution VI, NFR-15).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

DDL_V1 = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'reader',
    password_hash TEXT
);

CREATE TABLE IF NOT EXISTS highwater (
    source TEXT NOT NULL,
    item_key TEXT NOT NULL,
    last_key TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, item_key)
);

CREATE TABLE IF NOT EXISTS audit_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
    trigger TEXT NOT NULL DEFAULT 'manual',
    scheduled_by TEXT NOT NULL DEFAULT 'system',
    per_source_counts TEXT
);
"""

DDL_V2 = """
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    source TEXT NOT NULL,
    preset TEXT NOT NULL CHECK (preset IN ('daily', 'hourly', 'weekly', 'monthly', 'every-N-hours')),
    param INTEGER,
    fire_time TEXT NOT NULL DEFAULT '03:00',
    enabled INTEGER NOT NULL DEFAULT 1,
    next_fire_at TEXT NOT NULL,
    acl TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner, source, preset, param, fire_time)
);
"""

DDL_V3 = """
-- 1. accounts: two additive columns (001/002 rows gain '' defaults; no data
--    loss, no rewrite). `NOT NULL DEFAULT ''` keeps legacy rows valid and
--    keeps `ORDER BY last_active DESC` from special-casing NULL.
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
"""

AUDIT_STATUSES = ("ok", "partial", "failed")

DDL_V4 = """
-- audit_items: per-item extraction/write failure ledger (issue 4-A).
-- One row per (run_id, item_key) failure; supports targeted re-ingest
-- and coverage diagnostics.  Additive: new table only.
CREATE TABLE IF NOT EXISTS audit_items (
    run_id    TEXT NOT NULL,
    source     TEXT NOT NULL,
    item_key  TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error     TEXT,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_items_run
    ON audit_items (run_id, item_key);

-- ingest_locks: single-writer advisory lock (issue 5-A).
-- One row per source; a running pipeline holds a lease until exit.
-- Re-entry by the same run_id is safe (idempotent upsert).
CREATE TABLE IF NOT EXISTS ingest_locks (
    source     TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
"""



def apply_v1(conn) -> None:
    conn.executescript(DDL_V1)


def apply_v2(conn) -> None:
    conn.executescript(DDL_V2)


def apply_v3(conn) -> None:
    """Apply the v3 multi-user schema (additive: 3 new tables + 2 columns).

    Runs `DDL_V3` statement-by-statement. The CREATE TABLE/INDEX statements
    are already `IF NOT EXISTS`; the two `ALTER TABLE ... ADD COLUMN` lines
    are guarded by a column-existence check first, because SQLite has no
    `ADD COLUMN IF NOT EXISTS`. This keeps the step purely additive *and*
    safe to re-run (idempotent), which `migrate()` relies on.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    for stmt in _statements(DDL_V3):
        upper = stmt.lstrip().upper()
        if upper.startswith("ALTER TABLE"):
            # Guard each ADD COLUMN on its own column so a partial state
            # (one column already present) is handled correctly.
            target = "created_at" if "CREATED_AT" in upper else "last_active"
            if target in cols:
                continue
            conn.execute(stmt)
        else:
            conn.execute(stmt)


def apply_v4(conn) -> None:
    """Apply the v4 traceability schema (additive: 2 new tables).

    - audit_items (issue 4-A): per-item failure ledger
    - ingest_locks (issue 5-A): single-writer advisory lock

    Both are new tables (IF NOT EXISTS), safe to re-run.  Also closes
    any orphaned partial audit_runs rows (the two stale in-flight
    rows observed on OCI) by marking them failed with a remediation
    note — they represent runs that never completed and must not block
    the next run.
    """
    conn.executescript(DDL_V4)
    # Close stale partial audit_runs rows (started but never finished).
    conn.execute(
        "UPDATE audit_runs SET status='failed', "
        "completed_at=? WHERE status='partial'",
        (_now(),),
    )
DDL_V6 = """
-- 025 multi-tenant (apply_v6, user_version 4 -> 6).  Purely additive:
-- the two in-place table swaps (highwater, ingest_locks) copy every
-- legacy row into the tenant-partitioned primary key with
-- tenant_id = 1 (the install tenant).  No legacy data is dropped.

-- 1. tenants (T1 hard boundary: one Qdrant collection + one Neo4j DB
--    per tenant; slug is the deterministic naming key).
CREATE TABLE IF NOT EXISTS tenants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    slug             TEXT NOT NULL UNIQUE
                     CHECK (slug GLOB '[a-z0-9-]*'),
    name             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'suspended', 'deleted')),
    qdrant_collection TEXT NOT NULL,     -- "kb-" || slug, disjoint
                                          -- from reserved personal_kb / mem0
    neo4j_database   TEXT NOT NULL,      -- = slug; driver must pin ?db=
    created_at       TEXT NOT NULL,
    deleted_at       TEXT
);

-- 2. accounts gains the tenant (T3: legacy rows carry the install
--    tenant; install-level admin crosses tenants, tenant admins do not).
ALTER TABLE accounts ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1;

-- 3. audit runs/items gain the tenant (additive; legacy rows = install).
ALTER TABLE audit_runs ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE audit_items ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1;

-- 4. tenant_service_tokens (C-1 per-tenant analogue of the shared
--    system token: hash-only, no raw token at rest, revocable).
CREATE TABLE IF NOT EXISTS tenant_service_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   INTEGER NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,     -- pbkdf2$salt_hex$hash_hex (C-1)
    created_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tenant_service_tokens_tenant
    ON tenant_service_tokens (tenant_id);
"""

_INSTALL_TENANT = {
    "slug": "install",
    "name": "Install tenant",
    "status": "active",
    # Empty strings, not 'kb-install': the install tenant stays on the
    # legacy shared collection + default DB until the T2 backfill
    # retires them (task-11). The provision CLI rejects a create with
    # empty store names; only the seeded row carries them.
    "qdrant_collection": "",
    "neo4j_database": "",
    "created_at": None,  # filled with _now() at insert time
}


def apply_v6(conn) -> None:
    """Apply the v6 multi-tenant schema (025).

    Steps, all inside the migration transaction `migrate()` opens:

    1. DDL_V6 (tenants table, tenant_id on accounts/audit_runs/
       audit_items, tenant_service_tokens) statement-by-statement,
       with the v3-style ALTER guard so a partial step re-runs clean.
    2. Seed the install tenant (id 1, slug "install", active, empty
       qdrant_collection/neo4j_database: the install tenant uses the
       legacy shared collection + default DB until the T2 backfill
       retires them, task-11).
    3. In-place table swap of highwater:
       PK (source, item_key) -> (tenant_id, source, item_key).
       Legacy rows copied with tenant_id = 1.
    4. In-place table swap of ingest_locks:
       PK source -> (tenant_id, source).  Legacy rows copied with
       tenant_id = 1.

    Idempotent: guarded ALTERs + CREATE TABLE IF NOT EXISTS; the swap
    helpers are no-ops when the new primary key already exists.
    """
    for stmt in _statements(DDL_V6):
        upper = stmt.lstrip().upper()
        if upper.startswith("ALTER TABLE"):
            target = stmt.split()[2]  # ALTER TABLE <table> ...
            _add_column_if_missing(conn, target, "tenant_id")
        else:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO tenants (id, slug, name, status, qdrant_collection,"
        " neo4j_database, created_at) VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO NOTHING",
        (
            _INSTALL_TENANT["slug"],
            _INSTALL_TENANT["name"],
            _INSTALL_TENANT["status"],
            _INSTALL_TENANT["qdrant_collection"],
            _INSTALL_TENANT["neo4j_database"],
            _now(),
        ),
    )
    # In-place table swaps (no-op when already at the new PK).
    _table_swap(conn, "highwater",
                new_pk=("tenant_id", "source", "item_key"))
    _table_swap(conn, "ingest_locks",
                new_pk=("tenant_id", "source"))


def _add_column_if_missing(conn, table: str, column: str) -> None:
    """ALTER TABLE ... ADD COLUMN, guarded (SQLite has no IF NOT EXISTS)."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} "
            f"INTEGER NOT NULL DEFAULT 1"
        )


def _table_pk(conn, table: str):
    """Return the primary-key column sequence of `table` (in order)."""
    return [
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
        if row[5]  # row[5] is the non-zero pk position (1-based)
    ]


def _swap_to(conn, table: str, new_pk) -> None:
    """Copy `table` into a rebuilt copy with the new PK + tenant_id.

    Creates `table__new` with `tenant_id INTEGER NOT NULL` placed as
    the first PK column, copies every legacy row with `tenant_id = 1`,
    drops the old table, and renames the new one into place.  Runs
    inside the caller's transaction (migrate() commits at step end).
    """
    old_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    cols_to_copy = [c for c in old_cols if c != "tenant_id"]
    new_cols = ["tenant_id INTEGER NOT NULL"] + [
        _col_type(conn, table, c) for c in cols_to_copy
    ]
    conn.execute(
        f"CREATE TABLE {table}__new (\n    "
        + ",\n    ".join(new_cols)
        + f",\n    PRIMARY KEY ({', '.join(new_pk)})\n)"
    )
    conn.execute(
        f"INSERT INTO {table}__new ({', '.join(['tenant_id'] + cols_to_copy)}) "
        f"SELECT 1, {', '.join(cols_to_copy)} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")


def _col_type(conn, table: str, column: str) -> str:
    """Reconstruct a column declaration from PRAGMA table_info."""
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            col_type = row[2] or "TEXT"
            suffix = ""
            if row[3]:  # notnull
                suffix += " NOT NULL"
            if row[4]:  # default value
                suffix += f" DEFAULT {row[4]}"
            return f"{column} {col_type}{suffix}"
    raise ValueError(f"column {column!r} not in {table}")


def _table_swap(conn, table: str, new_pk) -> None:
    """Swap `table` to a tenant-partitioned primary key.

    `new_pk` is the tuple of PK column names in order (first column
    must be `tenant_id`).  No-op when the table already carries
    tenant_id (i.e. the swap has already happened).
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "tenant_id" in cols:
        return
    _swap_to(conn, table, new_pk)


def _statements(sql: str):
    """Split a multi-statement DDL block into individual statements.

    `--` line comments are stripped first so comment text (which may itself
    contain `;`) does not produce bogus partial statements.
    """
    lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        lines.append(line)
    for stmt in "\n".join(lines).split(";"):
        stmt = " ".join(stmt.split())  # normalise whitespace/newlines
        if stmt:
            yield stmt


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- highwater (resumability, NFR-9) --------------------------------------

def upsert_highwater(conn, source: str, item_key: str, last_key: str,
                     tenant_id: int = 1) -> None:
    """Record the last committed cursor for (tenant_id, source, item_key)."""
    conn.execute(
        "INSERT INTO highwater (tenant_id, source, item_key, last_key, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant_id, source, item_key) DO UPDATE SET "
        "last_key=excluded.last_key, updated_at=excluded.updated_at",
        (tenant_id, source, item_key, last_key, _now()),
    )
    conn.commit()


def get_highwater(conn, source: str, item_key: str,
                  tenant_id: int = 1):
    """Last committed cursor for (tenant_id, source, item_key), or None."""
    row = conn.execute(
        "SELECT last_key FROM highwater "
        "WHERE tenant_id=? AND source=? AND item_key=?",
        (tenant_id, source, item_key),
    ).fetchone()
    return row[0] if row else None


# --- audit runs (Constitution V: a row for every run, regardless of outcome)

def start_audit_run(conn, run_id: str, trigger: str = "manual",
                    scheduled_by: str = "system",
                    tenant_id: int = 1) -> None:
    """Insert the run's audit row up front as in-flight (`partial`)."""
    conn.execute(
        "INSERT INTO audit_runs (run_id, started_at, status, trigger, scheduled_by, tenant_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, _now(), "partial", trigger, scheduled_by, tenant_id),
    )
    conn.commit()


def finish_audit_run(conn, run_id: str, status: str, per_source_counts=None) -> None:
    """Close the run's audit row with its final status + per-source counts."""
    if status not in AUDIT_STATUSES:
        raise ValueError(f"status must be one of {AUDIT_STATUSES}, got {status!r}")
    conn.execute(
        "UPDATE audit_runs SET status=?, completed_at=?, per_source_counts=? "
        "WHERE run_id=?",
        (status, _now(), json.dumps(per_source_counts or {}), run_id),
    )
    conn.commit()


def record_audit_item(conn, run_id: str, source: str, item_key: str,
                      status: str, error: str | None = None) -> None:
    """Write one row to audit_items (issue 4-A failure ledger).

    Called from the pipeline when an item's extraction or write fails.
    Status is 'ok' or 'failed'.  The row is queryable for
    targeted re-ingest and coverage diagnostics.
    """
    conn.execute(
        "INSERT INTO audit_items (run_id, source, item_key, status, error, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, source, item_key, status, error, _now()),
    )
    conn.commit()


def acquire_ingest_lock(conn, source: str, run_id: str,
                       tenant_id: int = 1) -> bool:
    """Attempt to acquire the single-writer ingest lock for source.

    Returns True if the lock was acquired, False if another run already
    holds it.  The lock is released implicitly when the run finishes
    (or by explicit call to :func:).

    This is a simple DB-based advisory lock: the ingest_locks table
    has one row per (tenant_id, source).  A concurrent run whose
    INSERT fails (UNIQUE constraint) or whose SELECT shows a
    different run_id is rejected.
    """
    try:
        conn.execute(
            "INSERT INTO ingest_locks (tenant_id, source, run_id, acquired_at) "
            "VALUES (?, ?, ?, ?)",
            (tenant_id, source, run_id, _now()),
        )
        conn.commit()
        return True
    except Exception:
        # Row exists: check if it's our own run (re-entry) or another's.
        row = conn.execute(
            "SELECT run_id FROM ingest_locks WHERE tenant_id=? AND source=?",
            (tenant_id, source),
        ).fetchone()
        if row and row[0] == run_id:
            # Our own re-entry (e.g. sub-batch loop re-locks): OK.
            return True
        return False


def release_ingest_lock(conn, source: str, run_id: str,
                       tenant_id: int = 1) -> None:
    """Release the ingest lock for source (only if we hold it)."""
    conn.execute(
        "DELETE FROM ingest_locks WHERE tenant_id=? AND source=? AND run_id=?",
        (tenant_id, source, run_id),
    )
    conn.commit()


def list_runs(db, user: str | None = None, all_users: bool = False):
    """Query ``audit_runs`` with a "mine" filter (T019, US3).

    Parameters
    ----------
    db:
        Open 001/002/003 state connection (``audit_runs`` + ``accounts``
        tables required).
    user:
        The caller's email.  When set and ``all_users`` is False, returns
        only the runs where ``scheduled_by = user`` — the "mine" view.
        ``system``-attributed runs are naturally excluded because their
        ``scheduled_by`` is the literal string ``'system'``, which never
        matches a real user's email.
    all_users:
        When True, the caller must hold the ``view_all_history``
        capability (R3: admin only).  Returns **every** row in
        ``audit_runs`` (including system runs and other users' runs).
        If the caller's role lacks the capability, raises
        :class:`digital_twins.accounts.RoleDenied`.

    Returns
    -------
    list of tuples
        Each tuple is one ``audit_runs`` row in the order
        ``(run_id, started_at, completed_at, status, trigger,
        scheduled_by, per_source_counts)``.  Returns an empty list when
        no rows match.

    Raises
    ------
    RoleDenied
        If ``all_users=True`` but the caller's role lacks
        ``view_all_history`` (i.e. is not admin).

    Notes
    -----
    If both ``user`` is None and ``all_users`` is False, an empty list is
    returned (no view requested).  This is a safety default: callers
    should always pass ``user`` or set ``all_users=True``.
    """
    from ..accounts import get_role, guard

    if all_users:
        # Role check: the caller must hold view_all_history (R3: admin
        # only).  Uses accounts.get_role + accounts.guard so the
        # capability matrix is the single source of truth.
        if user is None:
            raise ValueError(
                "list_runs(all_users=True) requires a user email for the "
                "role check"
            )
        role = get_role(db, user)
        if role is None:
            raise ValueError(
                f"list_runs: unknown account {user!r} — cannot perform "
                "role check for all_users"
            )
        guard(role, "view_all_history")
        rows = db.execute(
            "SELECT run_id, started_at, completed_at, status, trigger, "
            "scheduled_by, per_source_counts FROM audit_runs "
            "ORDER BY started_at DESC"
        ).fetchall()
    elif user is not None:
        # "Mine" view: runs where scheduled_by matches the user's email.
        # system runs (scheduled_by='system') are excluded automatically
        # because no real user has the email 'system'.
        rows = db.execute(
            "SELECT run_id, started_at, completed_at, status, trigger, "
            "scheduled_by, per_source_counts FROM audit_runs "
            "WHERE scheduled_by=? ORDER BY started_at DESC",
            (user,),
        ).fetchall()
    else:
        # No user, no all_users: nothing to return.
        rows = []
    return rows
