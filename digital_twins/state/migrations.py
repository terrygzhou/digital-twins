"""Schema migrations keyed on `PRAGMA user_version`.

`migrate()` must complete before any command runs (constitution VI).

Each migration step runs in its own transaction (per-step `BEGIN`/`COMMIT`)
so a failure mid-step leaves the database in a valid state: the step's
changes are rolled back, and `user_version` only advances after the step
commits. A subsequent `migrate()` call re-runs the failed step from scratch.
"""

from .models import apply_v1, apply_v2

MIGRATIONS = [
    # (target user_version, apply function)
    (1, apply_v1),
    (2, apply_v2),
]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn) -> int:
    """Apply all pending migrations; return the resulting user_version.

    Each step is wrapped in a transaction: `BEGIN` before the apply
    function, `COMMIT` only after it succeeds and the PRAGMA advances.
    On failure the step's changes are rolled back and the exception
    propagates; the database remains at the previous user_version.
    """
    for target, apply in MIGRATIONS:
        if target > user_version(conn):
            try:
                conn.execute("BEGIN")
                apply(conn)
                conn.execute(f"PRAGMA user_version={target}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return user_version(conn)


def user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]
