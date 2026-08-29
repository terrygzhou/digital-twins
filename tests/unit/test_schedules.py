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
    """Fresh db → apply v1 then v2 lands on user_version 2 with the exact
    schedules column set (name, type, notnull, default, pk)."""
    conn = connect(tmp_path)
    models.apply_v1(conn)
    models.apply_v2(conn)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    assert migrations.user_version(conn) == 2
    assert migrations.SCHEMA_VERSION == 3

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
    """Apply v2 twice → no error AND schema stable (table_info
    identical; not just 'no exception')."""
    conn = connect(tmp_path)
    models.apply_v1(conn)
    models.apply_v2(conn)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    first = _table_info(conn)
    assert first, "precondition: schedules table present after first apply_v2"

    # re-run apply_v2: must be a no-op
    models.apply_v2(conn)
    second = _table_info(conn)
    assert second == first, "schema changed on re-apply (not idempotent)"
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

    # Now run the v2 step (v1 already applied; only v2 is pending).
    models.apply_v2(conn)
    conn.execute("PRAGMA user_version=2")
    conn.commit()

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


# ============================================================================
# T005 — CRUD + due filtering + claim_and_advance (contracts/scheduler.md)
# ============================================================================
#
# Time handling (data-model.md + brief ruling): the db stores AWARE UTC
# ISO-8601 strings for created_at / updated_at / next_fire_at. `expand_next`
# (T003) is pure local-time, so schedules.py converts to naive local ONLY at
# the expand_next boundary. Tests compare aware-UTC next_fire_at values
# directly (expand_next returns naive; the module wraps it back to aware UTC).

from datetime import datetime, timedelta as _timedelta, timezone

from digital_twins.scheduler import schedules
from digital_twins.scheduler.presets import expand_next


def _utc_iso(dt):
    """Render a naive-or-aware datetime as aware-UTC ISO-8601 (seconds),
    matching 001's _now() / the stored next_fire_at format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds")


def _naive_utc(dt):
    """Strip tzinfo from an aware-UTC datetime -> naive (for expand_next input)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _now_utc():
    return datetime.now(timezone.utc)


# --- create_schedule --------------------------------------------------------

def test_create_schedule_inserts_row(tmp_path):
    """create_schedule stores owner/source/preset; next_fire_at is a valid
    ISO-8601 aware-UTC string >= now; acl == 'owner'; enabled == 1;
    param NULL for daily."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    row = schedules.create_schedule(conn, "a@b.c", "fs", "daily",
                                    now=now)
    conn.commit()

    assert row["owner"] == "a@b.c"
    assert row["source"] == "fs"
    assert row["preset"] == "daily"
    assert row["param"] is None          # daily carries no param
    assert row["fire_time"] == "03:00"
    assert row["enabled"] == 1
    assert row["acl"] == "owner"

    # next_fire_at: valid aware-UTC ISO, strictly in the future.
    parsed = datetime.fromisoformat(row["next_fire_at"])
    assert parsed.tzinfo is not None, "next_fire_at must be aware-UTC"
    assert parsed >= now, "next_fire_at must be >= creation time"

    # The row is persisted in the table.
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
    conn.close()


def test_create_schedule_upsert_is_noop(tmp_path):
    """Re-adding the same (owner, source, preset, param, fire_time) returns
    the EXISTING row (same id), no duplicate, and does NOT reset
    next_fire_at."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    row1 = schedules.create_schedule(conn, "a@b.c", "fs", "daily",
                                     now=now)
    conn.commit()

    # Simulate the existing row having advanced (as claim_and_advance would).
    conn.execute(
        "UPDATE schedules SET next_fire_at = ?, updated_at = ? "
        "WHERE id = ?",
        ("2030-01-01T03:00:00+00:00", "2030-01-01T03:00:00+00:00", row1["id"]))
    conn.commit()

    row2 = schedules.create_schedule(conn, "a@b.c", "fs", "daily",
                                     now=now)
    conn.commit()

    # Exactly one row; the second call returns the existing row.
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
    assert row2["id"] == row1["id"]
    assert row2["next_fire_at"] == "2030-01-01T03:00:00+00:00", (
        "upsert must NOT reset next_fire_at (existing schedule keeps timing)")
    conn.close()


def test_create_schedule_invalid_preset(tmp_path):
    """A preset outside the 5-value set fails fast with ValueError."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    with pytest.raises(ValueError):
        schedules.create_schedule(conn, "a@b.c", "fs", "cron")
    conn.close()


def test_create_schedule_param_rules(tmp_path):
    """param rules: 'daily' with param -> ValueError; 'every-N-hours' with
    param=0 or param=None -> ValueError; param=2 -> OK."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    with pytest.raises(ValueError):
        schedules.create_schedule(conn, "a@b.c", "fs", "daily", param=1)

    with pytest.raises(ValueError):
        schedules.create_schedule(conn, "a@b.c", "fs", "every-N-hours",
                                  param=0)

    with pytest.raises(ValueError):
        schedules.create_schedule(conn, "a@b.c", "fs", "every-N-hours",
                                  param=None)

    ok = schedules.create_schedule(conn, "a@b.c", "fs", "every-N-hours",
                                   param=2)
    assert ok["param"] == 2
    conn.close()


# --- list_schedules ---------------------------------------------------------

def test_list_schedules_filters_by_owner(tmp_path):
    """list(owner='a') returns only a's schedules; list() returns all."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    schedules.create_schedule(conn, "a@b.c", "fs", "daily")
    schedules.create_schedule(conn, "b@b.c", "fs", "daily")
    conn.commit()

    only_a = schedules.list_schedules(conn, owner="a@b.c")
    assert len(only_a) == 1
    assert only_a[0]["owner"] == "a@b.c"

    all_rows = schedules.list_schedules(conn)
    assert len(all_rows) == 2
    assert {r["owner"] for r in all_rows} == {"a@b.c", "b@b.c"}
    conn.close()


# --- update_schedule --------------------------------------------------------

def test_update_schedule_recomputes_next_fire(tmp_path):
    """Changing fire_time from 03:00 to 05:00 recomputes next_fire_at from
    the ACTUAL now (clock-skew guard, R3) — NOT from the old next_fire_at —
    and bumps updated_at. Pin the guard: pre-set next_fire_at to a far-past
    value; after the update it must reflect the next 05:00 from now, not the
    stale value."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    row = schedules.create_schedule(conn, "a@b.c", "fs", "daily", now=now)
    conn.commit()

    # Stale next_fire_at (as if the clock jumped backward) and an older
    # updated_at so the bump is unambiguous within a same-second window.
    stale = "2020-01-01T03:00:00+00:00"
    old_updated = "2020-01-01T03:00:00+00:00"
    conn.execute(
        "UPDATE schedules SET next_fire_at = ?, updated_at = ? WHERE id = ?",
        (stale, old_updated, row["id"]))
    conn.commit()

    updated = schedules.update_schedule(conn, row["id"], fire_time="05:00")
    conn.commit()

    assert updated["fire_time"] == "05:00"
    # Recomputed from actual now, not the stale 2020 value.
    expected = _utc_iso(expand_next(
        "daily", None, "05:00", _naive_utc(now),
        _naive_utc(datetime.fromisoformat(row["created_at"]))))
    assert updated["next_fire_at"] == expected, (
        f"expected next 05:00 from now ({expected}), got "
        f"{updated['next_fire_at']!r} (stale={stale!r})")
    assert updated["next_fire_at"] != stale, "stale next_fire_at was reused"
    # updated_at bumped.
    assert updated["updated_at"] > old_updated
    conn.close()


def test_update_schedule_invalid_preset(tmp_path):
    """Updating to an unknown preset fails fast with ValueError; the row is
    unchanged."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    row = schedules.create_schedule(conn, "a@b.c", "fs", "daily")
    conn.commit()

    with pytest.raises(ValueError):
        schedules.update_schedule(conn, row["id"], preset="cron")
    conn.close()


# --- delete_schedule --------------------------------------------------------

def test_delete_schedule_removes_row(tmp_path):
    """delete_schedule removes the row (no row left; id absent)."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    row = schedules.create_schedule(conn, "a@b.c", "fs", "daily")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules").fetchone()[0] == 1

    schedules.delete_schedule(conn, row["id"])
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
    conn.close()


# --- due_schedules ----------------------------------------------------------

def test_due_schedules_filters_enabled_and_overdue(tmp_path):
    """Only enabled AND next_fire_at <= now schedules are due; a disabled due
    schedule and an enabled not-yet-due schedule are both excluded."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    # A: enabled, due (next_fire_at in the past).
    a = schedules.create_schedule(conn, "a@b.c", "fs", "daily", now=now)
    # B: enabled, not due (next_fire_at far in the future).
    b = schedules.create_schedule(conn, "b@b.c", "fs", "daily", now=now)
    # C: disabled, due.
    c = schedules.create_schedule(conn, "c@b.c", "fs", "daily", now=now)

    past = "2000-01-01T00:00:00+00:00"
    future = "2100-01-01T00:00:00+00:00"
    conn.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                 (past, a["id"]))
    conn.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                 (future, b["id"]))
    conn.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                 (past, c["id"]))
    conn.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (c["id"],))
    conn.commit()

    due = schedules.due_schedules(conn, now=now)
    assert [r["id"] for r in due] == [a["id"]], (
        f"only the enabled+due schedule (a={a['id']}) must be returned, "
        f"got {[r['id'] for r in due]}")
    conn.close()


def test_due_schedules_ordering(tmp_path):
    """Ordering: oldest next_fire_at first; on a tie, lower id first
    (ruling R-11)."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    s1 = schedules.create_schedule(conn, "a@b.c", "fs", "daily", now=now)
    s2 = schedules.create_schedule(conn, "b@b.c", "fs", "daily", now=now)
    s3 = schedules.create_schedule(conn, "c@b.c", "fs", "daily", now=now)

    # s3 oldest, s1 and s2 tied (s1 lower id -> s1 before s2).
    conn.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                 ("2000-01-01T03:00:00+00:00", s3["id"]))
    conn.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                 ("2000-01-02T03:00:00+00:00", s1["id"]))
    conn.execute("UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                 ("2000-01-02T03:00:00+00:00", s2["id"]))
    conn.commit()

    due = schedules.due_schedules(conn, now=now)
    assert [r["id"] for r in due] == [s3["id"], s1["id"], s2["id"]], (
        f"expected oldest-first, tie-break by id asc: "
        f"{[s3['id'], s1['id'], s2['id']]}, got {[r['id'] for r in due]}")
    conn.close()


# --- claim_and_advance ------------------------------------------------------

def test_claim_and_advance_moves_next_fire(tmp_path):
    """claim_and_advance sets next_fire_at = expand_next(preset, param,
    fire_time, fired_at, anchor=created_at) and bumps updated_at."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    row = schedules.create_schedule(conn, "a@b.c", "fs", "daily", now=now)
    conn.commit()

    fired_at = now + _timedelta(hours=5)
    old_next = row["next_fire_at"]
    old_updated = "2020-01-01T03:00:00+00:00"
    conn.execute(
        "UPDATE schedules SET updated_at = ? WHERE id = ?",
        (old_updated, row["id"]))
    conn.commit()

    schedules.claim_and_advance(conn, row["id"], fired_at)
    conn.commit()

    anchor = datetime.fromisoformat(row["created_at"])
    expected = _utc_iso(expand_next(
        "daily", None, "03:00", _naive_utc(fired_at), _naive_utc(anchor)))
    got = conn.execute(
        "SELECT next_fire_at, updated_at FROM schedules WHERE id = ?",
        (row["id"],)).fetchone()
    assert got[0] == expected, f"expected {expected!r}, got {got[0]!r}"
    assert got[1] > old_updated, "updated_at must be bumped"
    # (No "must differ from old_next" assertion: a daily 03:00 fired at
    # now+5h where 03:00 already passed today legitimately lands on the same
    # next day-03:00 that creation computed. The equality-with-expand_next
    # formula above is the meaningful check.)
    conn.close()


def test_claim_and_advance_hourly(tmp_path):
    """hourly: fired at T -> next_fire_at == T + 1h (param/anchor
    irrelevant for hourly)."""
    conn = connect(tmp_path)
    migrations.migrate(conn)

    now = _now_utc()
    row = schedules.create_schedule(conn, "a@b.c", "fs", "hourly", now=now)
    conn.commit()

    fired_at = now + _timedelta(hours=5)
    schedules.claim_and_advance(conn, row["id"], fired_at)
    conn.commit()

    expected = _utc_iso(_naive_utc(fired_at) + _timedelta(hours=1))
    got = conn.execute(
        "SELECT next_fire_at FROM schedules WHERE id = ?",
        (row["id"],)).fetchone()[0]
    assert got == expected, f"expected {expected!r}, got {got!r}"
    conn.close()
