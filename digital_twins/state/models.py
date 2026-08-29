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

def upsert_highwater(conn, source: str, item_key: str, last_key: str) -> None:
    """Record the last committed cursor for (source, item_key)."""
    conn.execute(
        "INSERT INTO highwater (source, item_key, last_key, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (source, item_key) DO UPDATE SET "
        "last_key=excluded.last_key, updated_at=excluded.updated_at",
        (source, item_key, last_key, _now()),
    )
    conn.commit()


def get_highwater(conn, source: str, item_key: str):
    """Last committed cursor for (source, item_key), or None."""
    row = conn.execute(
        "SELECT last_key FROM highwater WHERE source=? AND item_key=?",
        (source, item_key),
    ).fetchone()
    return row[0] if row else None


# --- audit runs (Constitution V: a row for every run, regardless of outcome)

def start_audit_run(conn, run_id: str, trigger: str = "manual",
                    scheduled_by: str = "system") -> None:
    """Insert the run's audit row up front as in-flight (`partial`)."""
    conn.execute(
        "INSERT INTO audit_runs (run_id, started_at, status, trigger, scheduled_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, _now(), "partial", trigger, scheduled_by),
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
