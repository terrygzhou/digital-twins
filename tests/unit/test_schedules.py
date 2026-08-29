"""Migration v2: `schedules` table (002-scheduled-runs, T004).

Pins the DDL column-by-column (data-model.md), the UNIQUE(owner, source,
preset, param, fire_time) constraint, the preset 5-value CHECK, idempotency
(apply twice = schema-stable no-op), and 001 data preservation (v1 rows
survive the v1→v2 upgrade; schedules starts empty).

T005 (CRUD) builds on this table; these tests are the migration contract.
"""

import sqlite3

import pytest

from digital_twins.state import migrations, models
from digital_twins.state.db import connect


# The exact column contract from data-model.md, as a name → (type, notnull,
# default) map. PRAGMA table_info returns (cid, name, type, notnull, dflt, pk).
EXPECTED_COLUMNS = {
    "id":          ("INTEGER", 0, None,    1),
    "owner":       ("TEXT",    1, None,    0),
    "source":      ("TEXT",    1, None,    0),
    "preset":      ("TEXT",    1, None,    0),
    "param":       ("INTEGER", 0, None,    0),
    "fire_time":   ("TEXT",    1, "'03:00'", 0),
    "enabled":     ("INTEGER", 1, "1",      0),
    "next_fire_at":("TEXT",    1, None,    0),
    "acl":         ("TEXT",    1, "'owner'", 0),
    "created_at":  ("TEXT",    1, None,    0),
    "updated_at":  ("TEXT",    1, None,    0),
}

VALID_PRESETS = ("daily", "hourly", "weekly", "monthly", "every-N-hours")


def _table_info(conn):
    """schedules columns as {name: (type, notnull, default, pk)} or {} if absent."""
    rows = conn.execute("PRAGMA table_info(schedules)").fetchall()
    if not rows:
        return {}
    return {r[1]: (r[2], r[3], r[4], r[5]) for r in rows}


def _insert_schedule(conn, *, owner="alice@example.com", source="fs",
                     preset="daily", param=None, fire_time="03:00"):
    conn.execute(
        "INSERT INTO schedules "
        "(owner, source, preset, param, fire_time, enabled, next_fire_at, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (owner, source, preset, param, fire_time,
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()


# 1 — clean apply -----------------------------------------------------------

def test_migration_v2_applies_cleanly(tmp_path):
    """Fresh db → migrate() lands on SCHEMA_VERSION 2 with the exact
    schedules column set (name, type, notnull, default, pk)."""
    conn = connect(tmp_path)
    v = migrations.migrate(conn)
    assert v == 2
    assert migrations.SCHEMA_VERSION == 2

    cols = _table_info(conn)
    assert cols, "schedules table must exist after v2 migration"
    assert set(cols) == set(EXPECTED_COLUMNS), (
        f"column set mismatch: got {sorted(cols)}, want {sorted(EXPECTED_COLUMNS)}"
    )
    for name, (type_, notnull, dflt, pk) in EXPECTED_COLUMNS.items():
        got = cols[name]
        assert got[0] == type_, f"{name}: type {got[0]!r} != {type_!r}"
        assert got[1] == notnull, f"{name}: notnull {got[1]} != {notnull}"
        assert got[2] == dflt, f"{name}: default {got[2]!r} != {dflt!r}"
        assert got[3] == pk, f"{name}: pk {got[3]} != {pk}"
    conn.close()


# 2 — idempotency -----------------------------------------------------------

def test_migration_v2_idempotent(tmp_path):
    """Apply migrate() twice → no error AND schema stable (table_info
    identical; not just 'no exception')."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    first = _table_info(conn)
    assert first, "precondition: schedules table present after first migrate"

    # re-run: must be a no-op
    v = migrations.migrate(conn)
    assert v == 2
    second = _table_info(conn)
    assert second == first, "schema changed on re-migrate (not idempotent)"
    # user_version must not advance past 2
    assert migrations.user_version(conn) == 2
    conn.close()


# 3 — 001 data preservation -------------------------------------------------

def test_migration_v2_preserves_v1_data(tmp_path):
    """Seed 001 v1 tables, run migrate, assert v1 rows intact AND schedules
    starts empty (the new table is created, not back-filled)."""
    conn = connect(tmp_path)
    # Force a v1-only db first: drop to user_version 0, apply v1 manually.
    # (connect() starts at user_version 0; apply only v1.)
    models.apply_v1(conn)
    conn.execute("PRAGMA user_version=1")
    conn.commit()

    models.upsert_highwater(conn, "fs", "a.txt", "abc123")
    models.start_audit_run(conn, "run-x", trigger="manual")
    models.finish_audit_run(conn, "run-x", "ok")
    conn.execute(
        "INSERT INTO accounts (email, role) VALUES ('a@b.c', 'reader')")
    conn.commit()

    hw_before = conn.execute(
        "SELECT source, item_key, last_key FROM highwater").fetchall()
    runs_before = conn.execute(
        "SELECT run_id, status FROM audit_runs").fetchall()
    acct_before = conn.execute(
        "SELECT email, role FROM accounts").fetchall()

    # Now run the full migration (v1 already applied; only v2 is pending).
    v = migrations.migrate(conn)
    assert v == 2

    # v1 data must be intact
    assert conn.execute(
        "SELECT source, item_key, last_key FROM highwater").fetchall() == hw_before
    assert conn.execute(
        "SELECT run_id, status FROM audit_runs").fetchall() == runs_before
    assert conn.execute(
        "SELECT email, role FROM accounts").fetchall() == acct_before

    # schedules must exist and be empty
    assert _table_info(conn), "schedules table must exist after v2"
    assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
    conn.close()


# 4 — uniqueness constraint -------------------------------------------------

def test_schedule_uniqueness_constraint(tmp_path):
    """Two schedules with the same (owner, source, preset, param, fire_time)
    → the second INSERT raises. Pins the UNIQUE constraint T005's upsert
    will rely on. Uses param=1 (non-NULL) because SQLite treats NULLs as
    distinct in UNIQUE constraints — a NULL param would never collide."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    _insert_schedule(conn, owner="a@b.c", source="fs", preset="every-N-hours",
                     param=1, fire_time="03:00")
    with pytest.raises(sqlite3.IntegrityError,
                       match="UNIQUE constraint failed"):
        _insert_schedule(conn, owner="a@b.c", source="fs",
                         preset="every-N-hours", param=1, fire_time="03:00")
    conn.close()


def test_schedule_uniqueness_allows_distinct_keys(tmp_path):
    """Distinct (owner, source, preset, param, fire_time) tuples all insert
    cleanly — the UNIQUE key is the full 5-tuple, not a prefix."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    _insert_schedule(conn, owner="a@b.c", source="fs", preset="daily",
                     param=None, fire_time="03:00")
    # differ on owner
    _insert_schedule(conn, owner="c@d.e", source="fs", preset="daily",
                     param=None, fire_time="03:00")
    # differ on source
    _insert_schedule(conn, owner="a@b.c", source="imap", preset="daily",
                     param=None, fire_time="03:00")
    # differ on preset
    _insert_schedule(conn, owner="a@b.c", source="fs", preset="hourly",
                     param=None, fire_time="03:00")
    # differ on param
    _insert_schedule(conn, owner="a@b.c", source="fs", preset="every-N-hours",
                     param=2, fire_time="03:00")
    _insert_schedule(conn, owner="a@b.c", source="fs", preset="every-N-hours",
                     param=3, fire_time="03:00")
    # differ on fire_time
    _insert_schedule(conn, owner="a@b.c", source="fs", preset="daily",
                     param=None, fire_time="04:00")

    count = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
    assert count == 7, f"expected 7 distinct rows, got {count}"
    conn.close()


# 5 — preset CHECK constraint ----------------------------------------------

@pytest.mark.parametrize("preset", VALID_PRESETS)
def test_preset_check_constraint_accepts_valid(tmp_path, preset):
    """Each of the 5 legal presets inserts cleanly."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    # every-N-hours needs a param; others leave param NULL.
    param = 1 if preset == "every-N-hours" else None
    _insert_schedule(conn, owner="a@b.c", source="fs", preset=preset,
                     param=param, fire_time="03:00")
    conn.close()


@pytest.mark.parametrize("bad_preset", ["cron-daily", "biweekly", "", "DAILY"])
def test_preset_check_constraint_rejects_invalid(tmp_path, bad_preset):
    """Any preset outside the 5-value set raises IntegrityError."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_schedule(conn, owner="a@b.c", source="fs",
                         preset=bad_preset, param=None, fire_time="03:00")
    conn.close()
