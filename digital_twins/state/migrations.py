"""Schema migrations keyed on `PRAGMA user_version`.

`migrate()` must complete before any command runs (constitution VI).
"""

from .models import apply_v1

MIGRATIONS = [
    # (target user_version, apply function)
    (1, apply_v1),
]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn) -> int:
    """Apply all pending migrations; return the resulting user_version."""
    for target, apply in MIGRATIONS:
        if target > user_version(conn):
            apply(conn)
            conn.execute(f"PRAGMA user_version={target}")
            conn.commit()
    return user_version(conn)


def user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]
