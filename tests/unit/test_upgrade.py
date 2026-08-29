"""Upgrade preservation: seed state, run a version-bumped migration,
assert state + accounts + config survive (S7, NFR-15, SC-006, FR-012).

Constitution VI: migration completes before any new code runs.
"""

import json

import pytest

from digital_twins.state import migrations, models
from digital_twins.state.db import connect


def _apply_v2_marker_table(conn):
    """v2 migration that adds a new table (trivial schema change)."""
    conn.execute("CREATE TABLE IF NOT EXISTS v2_marker (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO v2_marker (id) VALUES (1)")


def _apply_v3_noop(conn):
    """No-op v3 migration: data must survive."""
    pass


def _snapshot_all(conn):
    """Capture every row in every table (v1 + v2) as a dict-of-lists.

    Includes `schedules` (the v2 table, T004) so the "no data loss on
    migration" invariant covers the new table. `schedules` is empty/absent
    before the v2 step runs and is empty after it (v2 only creates the
    table, no back-fill), so it is `[]` on both sides of the migration.
    """
    return {
        "accounts": conn.execute(
            "SELECT * FROM accounts ORDER BY id"
        ).fetchall(),
        "highwater": conn.execute(
            "SELECT * FROM highwater ORDER BY source, item_key"
        ).fetchall(),
        "audit_runs": conn.execute(
            "SELECT * FROM audit_runs ORDER BY run_id"
        ).fetchall(),
        "schedules": _schedules_rows(conn),
    }


def _schedules_rows(conn):
    """Rows in `schedules`, or [] if the table does not exist yet (pre-v2)."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "schedules" not in tables:
        return []
    return conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()


def _seed_v2(conn):
    """Populate the v2 `schedules` table with a representative row.

    Mirrors test_schedules.py's insert so the v2→v3 "no data loss" invariant
    covers the v2 table with real content, not just an empty table.
    """
    conn.execute(
        "INSERT INTO schedules "
        "(owner, source, preset, param, fire_time, enabled, next_fire_at, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("alice@example.com", "fs", "daily", None, "03:00", 1,
         "2026-01-02T03:00:00+00:00",
         "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()


def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _pin_v1(conn):
    """Force the database to user_version 1 (v1-only state).

    Real v2 now exists (T004), so a plain `migrate()` on a fresh db lands
    on v2. These tests exercise a v1→v2 upgrade, so they pin the db back to
    v1 first: run the full migration, then drop the schedules table and
    rewind user_version to 1.
    """
    migrations.migrate(conn)
    conn.execute("DROP TABLE IF EXISTS schedules")
    conn.execute("PRAGMA user_version=1")
    conn.commit()


def _seed_v1(conn):
    """Populate all three v1 tables with representative rows."""
    conn.execute(
        "INSERT INTO accounts (email, role, password_hash) "
        "VALUES ('alice@example.com', 'admin', 'pbkdf2:abc123')"
    )
    conn.execute(
        "INSERT INTO accounts (email, role, password_hash) "
        "VALUES ('bob@example.com', 'reader', NULL)"
    )
    conn.commit()

    models.upsert_highwater(conn, "fs", "notes/a.txt", "abc123")
    models.upsert_highwater(conn, "imap", "inbox", "msg-id-42")
    models.upsert_highwater(conn, "fs", "logs/b.log", "xyz789")

    models.start_audit_run(conn, "run-001", trigger="schedule",
                           scheduled_by="cron-e4735")
    models.finish_audit_run(conn, "run-001", "ok",
                            per_source_counts={"fs": {"new": 2, "skipped": 1, "failed": 0}})
    models.start_audit_run(conn, "run-002", trigger="manual")
    models.finish_audit_run(conn, "run-002", "failed",
                            per_source_counts={"imap": {"new": 0, "skipped": 0, "failed": 1}})


# 1 ------------------------------------------------------------------------

def test_seed_data_survives_version_bumped_migration(tmp_path):
    """All v1 rows are intact after a full migrate() to v3 (S7, NFR-15)."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)
    before = _snapshot_all(conn)

    v = migrations.migrate(conn)
    assert v == 3, "user_version must advance to 3"

    after = _snapshot_all(conn)
    for table in ("accounts", "highwater", "audit_runs", "schedules"):
        assert after[table] == before[table], (
            f"{table} rows changed after migration to v3"
        )
    conn.close()


# 2 ------------------------------------------------------------------------

def test_accounts_survive_upgrade(tmp_path):
    """Account rows survive a v1→v2 upgrade with all fields intact."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    accounts_before = conn.execute(
        "SELECT email, role, password_hash FROM accounts ORDER BY email"
    ).fetchall()
    assert len(accounts_before) == 2, "precondition: two accounts seeded"

    migrations.migrate(conn)

    accounts_after = conn.execute(
        "SELECT email, role, password_hash FROM accounts ORDER BY email"
    ).fetchall()
    assert accounts_after == accounts_before, "account data changed"

    admin = conn.execute(
        "SELECT role, password_hash FROM accounts WHERE email='alice@example.com'"
    ).fetchone()
    assert admin == ("admin", "pbkdf2:abc123")

    reader = conn.execute(
        "SELECT role, password_hash FROM accounts WHERE email='bob@example.com'"
    ).fetchone()
    assert reader == ("reader", None)
    conn.close()


# 3 ------------------------------------------------------------------------

def test_highwater_survives_upgrade(tmp_path):
    """High-water marks are unchanged after a v1→v2 upgrade (NFR-9)."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    hw_before = conn.execute(
        "SELECT source, item_key, last_key FROM highwater "
        "ORDER BY source, item_key"
    ).fetchall()
    assert len(hw_before) == 3, "precondition: three highwater rows seeded"

    migrations.migrate(conn)

    hw_after = conn.execute(
        "SELECT source, item_key, last_key FROM highwater "
        "ORDER BY source, item_key"
    ).fetchall()
    assert hw_after == hw_before, "highwater data changed"

    assert models.get_highwater(conn, "fs", "notes/a.txt") == "abc123"
    assert models.get_highwater(conn, "imap", "inbox") == "msg-id-42"
    assert models.get_highwater(conn, "fs", "logs/b.log") == "xyz789"
    conn.close()


# 4 ------------------------------------------------------------------------

def test_audit_runs_survive_upgrade(tmp_path):
    """Audit run rows are unchanged after a v1→v2 upgrade (Constitution V)."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    runs_before = conn.execute(
        "SELECT run_id, started_at, status, trigger, scheduled_by, "
        "completed_at, per_source_counts FROM audit_runs ORDER BY run_id"
    ).fetchall()
    assert len(runs_before) == 2, "precondition: two audit runs seeded"

    migrations.migrate(conn)

    runs_after = conn.execute(
        "SELECT run_id, started_at, status, trigger, scheduled_by, "
        "completed_at, per_source_counts FROM audit_runs ORDER BY run_id"
    ).fetchall()
    assert runs_after == runs_before, "audit run data changed"

    r1 = conn.execute(
        "SELECT status, trigger, scheduled_by FROM audit_runs WHERE run_id='run-001'"
    ).fetchone()
    assert r1 == ("ok", "schedule", "cron-e4735")
    r1_counts = json.loads(
        conn.execute(
            "SELECT per_source_counts FROM audit_runs WHERE run_id='run-001'"
        ).fetchone()[0]
    )
    assert r1_counts == {"fs": {"new": 2, "skipped": 1, "failed": 0}}

    r2 = conn.execute(
        "SELECT status FROM audit_runs WHERE run_id='run-002'"
    ).fetchone()
    assert r2[0] == "failed"
    conn.close()


# 5 ------------------------------------------------------------------------

def test_migration_is_idempotent(tmp_path):
    """Running migrate() twice is a no-op; user_version is unchanged."""
    conn = connect(tmp_path)
    v1 = migrations.migrate(conn)
    v2 = migrations.migrate(conn)
    v3 = migrations.migrate(conn)
    assert v1 == v2 == v3 == 3
    assert migrations.user_version(conn) == 3
    conn.close()


def test_migration_is_idempotent_after_upgrade(tmp_path):
    """After a v1→v3 upgrade, re-running migrate() is a no-op."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    v = migrations.migrate(conn)
    assert v == 3

    v_again = migrations.migrate(conn)
    assert v_again == 3, "re-running migrate() must not advance version"
    assert migrations.user_version(conn) == 3

    # data still intact
    assert len(conn.execute("SELECT * FROM accounts").fetchall()) == 2
    assert len(conn.execute("SELECT * FROM highwater").fetchall()) == 3
    assert len(conn.execute("SELECT * FROM audit_runs").fetchall()) == 2
    conn.close()


# 6 ------------------------------------------------------------------------

def test_migrations_apply_in_order(tmp_path, monkeypatch):
    """v2 then v3 apply in order; user_version lands on 3; all data intact."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    applied = []

    def apply_v2_track(conn_):
        applied.append("v2")
        models.apply_v2(conn_)

    def apply_v3_track(conn_):
        applied.append("v3")
        models.apply_v3(conn_)

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (2, apply_v2_track),
        (3, apply_v3_track),
    ])
    v = migrations.migrate(conn)
    assert v == 3, "user_version must advance to 3"
    assert applied == ["v2", "v3"], "migrations must apply in order"

    # v2 table exists; v3 tables exist
    tables = _table_names(conn)
    assert "schedules" in tables, "v2 schedules table must exist"
    assert {"personal_tokens", "user_config", "sessions"} <= tables

    # all v1 data intact
    assert len(conn.execute("SELECT * FROM accounts").fetchall()) == 2
    assert len(conn.execute("SELECT * FROM highwater").fetchall()) == 3
    assert len(conn.execute("SELECT * FROM audit_runs").fetchall()) == 2

    # re-running is a no-op
    v_again = migrations.migrate(conn)
    assert v_again == 3
    assert applied == ["v2", "v3"], "no re-application on re-run"
    conn.close()


# T002: v2 → v3 migration (personal_tokens, user_config, sessions + 2
# accounts columns). Mirrors the 001/002 test_upgrade.py pattern (S7,
# NFR-15) — additive-only, no data loss, idempotent.

def _pin_v2(conn):
    """Force the database to user_version 2 (v2-only state).

    A plain `migrate()` now lands on v3 (T002), so these tests exercise a
    v2→v3 upgrade: apply v1 + v2 explicitly, then rewind user_version to 2.
    """
    models.apply_v1(conn)
    models.apply_v2(conn)
    conn.execute("PRAGMA user_version=2")
    conn.commit()


def test_v3_adds_new_tables_and_version(tmp_path):
    """v2→v3 creates personal_tokens/user_config/sessions; user_version=3."""
    conn = connect(tmp_path)
    _pin_v2(conn)

    v = migrations.migrate(conn)
    assert v == 3, "user_version must advance to 3"
    assert migrations.user_version(conn) == 3

    tables = _table_names(conn)
    assert {"personal_tokens", "user_config", "sessions"} <= tables, (
        f"v3 tables missing: {sorted(tables)}"
    )
    conn.close()


def test_v3_accounts_columns_with_empty_defaults(tmp_path):
    """v2→v3 adds accounts.created_at / last_active, NOT NULL DEFAULT ''."""
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v1(conn)

    migrations.migrate(conn)

    cols = {r[1]: (r[2], r[3], r[4]) for r in conn.execute(
        "PRAGMA table_info(accounts)")}
    assert cols.get("created_at") == ("TEXT", 1, "''"), (
        f"created_at: got {cols.get('created_at')}")
    assert cols.get("last_active") == ("TEXT", 1, "''"), (
        f"last_active: got {cols.get('last_active')}")

    # Legacy v1/v2 rows gain the '' sentinel for both new columns.
    for email in ("alice@example.com", "bob@example.com"):
        row = conn.execute(
            "SELECT created_at, last_active FROM accounts WHERE email=?",
            (email,)).fetchone()
        assert row == ("", ""), f"legacy row {email} not defaulted: {row}"
    conn.close()


def test_v2_seeded_rows_survive_v3(tmp_path):
    """All v1 + v2 rows are byte-identical after the v2→v3 upgrade (NFR-15)."""
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v1(conn)
    _seed_v2(conn)

    accounts_before = conn.execute(
        "SELECT id, email, role, password_hash FROM accounts ORDER BY id"
    ).fetchall()
    highwater_before = conn.execute(
        "SELECT * FROM highwater ORDER BY source, item_key").fetchall()
    audit_before = conn.execute(
        "SELECT * FROM audit_runs ORDER BY run_id").fetchall()
    schedules_before = conn.execute(
        "SELECT * FROM schedules ORDER BY id").fetchall()

    assert len(accounts_before) == 2
    assert len(highwater_before) == 3
    assert len(audit_before) == 2
    assert len(schedules_before) == 1, "precondition: one schedule seeded"

    v = migrations.migrate(conn)
    assert v == 3

    assert conn.execute(
        "SELECT id, email, role, password_hash FROM accounts ORDER BY id"
    ).fetchall() == accounts_before, "accounts rows changed"
    assert conn.execute(
        "SELECT * FROM highwater ORDER BY source, item_key"
    ).fetchall() == highwater_before, "highwater rows changed"
    assert conn.execute(
        "SELECT * FROM audit_runs ORDER BY run_id"
    ).fetchall() == audit_before, "audit_runs rows changed"
    assert conn.execute(
        "SELECT * FROM schedules ORDER BY id"
    ).fetchall() == schedules_before, "schedules rows changed"
    conn.close()


def test_v3_is_idempotent(tmp_path):
    """Applying v3 twice is a no-op: no error, no duplicate tables/columns."""
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v1(conn)
    _seed_v2(conn)

    v1 = migrations.migrate(conn)
    assert v1 == 3
    tables_after_first = _table_names(conn)
    acct_cols_after_first = {r[1] for r in conn.execute(
        "PRAGMA table_info(accounts)")}

    # Second full migrate() must not error nor duplicate anything.
    v2 = migrations.migrate(conn)
    assert v2 == 3, "re-run must not advance user_version past 3"
    assert migrations.user_version(conn) == 3

    assert _table_names(conn) == tables_after_first, (
        "re-running v3 changed the table set")
    acct_cols_after_second = {r[1] for r in conn.execute(
        "PRAGMA table_info(accounts)")}
    assert acct_cols_after_second == acct_cols_after_first, (
        "re-running v3 changed the accounts columns")

    # calling apply_v3 directly a second time is also a no-op (additive DDL).
    models.apply_v3(conn)
    conn.close()


def test_fresh_install_migrates_to_v3(tmp_path):
    """Fresh db: migrate() runs v1→v2→v3 and lands on v3 with all tables."""
    conn = connect(tmp_path)
    v = migrations.migrate(conn)
    assert v == 3
    assert migrations.SCHEMA_VERSION == 3

    tables = _table_names(conn)
    assert {"accounts", "highwater", "audit_runs", "schedules",
            "personal_tokens", "user_config", "sessions"} <= tables
    conn.close()


# FR-012: config must survive an in-place upgrade (stays on disk) -----------

def test_config_file_survives_upgrade(tmp_path):
    """kb.local.yml on disk is untouched by the migration process (FR-012)."""
    config = tmp_path / "kb.local.yml"
    config.write_text("state_dir: /tmp/kb-state\nembedding_dim: 384\n")
    original = config.read_text()

    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    migrations.migrate(conn)

    assert config.read_text() == original, "config file must not be modified"
    assert config.exists(), "config file must not be deleted"
    conn.close()


# T034: transaction safety — a failing migration step rolls back -----------

def test_failed_migration_step_rolls_back(tmp_path, monkeypatch):
    """A migration step that fails does not advance user_version:
    the PRAGMA is only set after the apply function succeeds, so a
    mid-step failure leaves user_version at the previous value.
    (SQLite DDL is autocommit and cannot be rolled back — the invariant
    is that user_version does not advance, not that partial DDL is
    reverted. A subsequent migrate() re-runs the step from scratch.)"""
    conn = connect(tmp_path)
    _pin_v1(conn)  # force v1-only state so the failing v2 step is pending
    _seed_v1(conn)

    def apply_v2_failing(conn_):
        conn_.execute("CREATE TABLE IF NOT EXISTS v2_partial (id INTEGER PRIMARY KEY)")
        conn_.execute("INSERT INTO v2_partial (id) VALUES (1)")
        raise RuntimeError("simulated mid-step failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (2, apply_v2_failing),
    ])

    # First attempt: v2 fails, user_version does not advance
    with pytest.raises(RuntimeError, match="simulated mid-step failure"):
        migrations.migrate(conn)

    # user_version must still be 1 (the failed step did not advance it)
    assert migrations.user_version(conn) == 1, (
        "user_version must not advance on a failed migration step")

    # v1 data is intact
    assert len(conn.execute("SELECT * FROM accounts").fetchall()) == 2

    # Now restore the real MIGRATIONS (undo the failing-v2 patch): the
    # pending v2 + v3 steps should apply cleanly (v1 already applied,
    # user_version at 1). A full migrate() now lands on v3.
    monkeypatch.undo()
    v = migrations.migrate(conn)
    assert v == 3, "a subsequent migrate() must apply the now-working steps"
    conn.close()
