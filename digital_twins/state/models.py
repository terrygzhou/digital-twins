"""State tables: accounts, highwater, audit_runs (data-model.md)."""

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

AUDIT_STATUSES = ("ok", "partial", "failed")


def apply_v1(conn) -> None:
    conn.executescript(DDL_V1)


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
