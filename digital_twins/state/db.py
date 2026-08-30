"""SQLite state DB (stdlib sqlite3, WAL) in the state dir.

State-dir resolution itself lives in the config layer (state_dir knob);
this module receives the resolved path and owns the connection.

``connect`` returns the schema already migrated to the current version
(idempotent, constitution VI): a fresh state dir gets the full v3 schema
(``accounts``/``sessions``/``audit_runs``/``highwater``) on open, so
callers can use the connection without a separate migrate step.  The
CLI's ``pre_command`` migration hook and any explicit
``migrations.migrate`` calls stay no-ops for an up-to-date DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import migrations

DB_FILENAME = "state.db"


def state_db_path(state_dir) -> Path:
    """Where the state DB lives for a resolved state dir."""
    return Path(state_dir) / DB_FILENAME


def connect(state_dir) -> sqlite3.Connection:
    """Open (creating if needed) the state DB with WAL journal mode.

    The connection is migrated to ``migrations.SCHEMA_VERSION`` on open
    (a no-op when already up to date).
    """
    path = state_db_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrations.migrate(conn)
    return conn
