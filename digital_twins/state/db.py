"""SQLite state DB (stdlib sqlite3, WAL) in the state dir.

State-dir resolution itself lives in the config layer (state_dir knob);
this module receives the resolved path and owns the connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_FILENAME = "state.db"


def state_db_path(state_dir) -> Path:
    """Where the state DB lives for a resolved state dir."""
    return Path(state_dir) / DB_FILENAME


def connect(state_dir) -> sqlite3.Connection:
    """Open (creating if needed) the state DB with WAL journal mode."""
    path = state_db_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
