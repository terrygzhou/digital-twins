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
    """All v1 rows are intact after a v1→v2 upgrade (S7, NFR-15)."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)
    before = _snapshot_all(conn)

    v = migrations.migrate(conn)
    assert v == 2, "user_version must advance to 2"

    after = _snapshot_all(conn)
    for table in ("accounts", "highwater", "audit_runs", "schedules"):
        assert after[table] == before[table], (
            f"{table} rows changed after v2 migration"
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
    assert v1 == v2 == v3 == 2
    assert migrations.user_version(conn) == 2
    conn.close()


def test_migration_is_idempotent_after_upgrade(tmp_path):
    """After a v1→v2 upgrade, re-running migrate() is a no-op."""
    conn = connect(tmp_path)
    _pin_v1(conn)
    _seed_v1(conn)

    v = migrations.migrate(conn)
    assert v == 2

    v_again = migrations.migrate(conn)
    assert v_again == 2, "re-running migrate() must not advance version"
    assert migrations.user_version(conn) == 2

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
        _apply_v2_marker_table(conn_)

    def apply_v3_track(conn_):
        applied.append("v3")
        _apply_v3_noop(conn_)

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (2, apply_v2_track),
        (3, apply_v3_track),
    ])
    v = migrations.migrate(conn)
    assert v == 3, "user_version must advance to 3"
    assert applied == ["v2", "v3"], "migrations must apply in order"

    # v2 marker table exists with expected content
    marker = conn.execute(
        "SELECT id FROM v2_marker ORDER BY id"
    ).fetchall()
    assert marker == [(1,)], "v2 marker table must exist with one row"

    # all v1 data intact
    assert len(conn.execute("SELECT * FROM accounts").fetchall()) == 2
    assert len(conn.execute("SELECT * FROM highwater").fetchall()) == 3
    assert len(conn.execute("SELECT * FROM audit_runs").fetchall()) == 2

    # re-running is a no-op
    v_again = migrations.migrate(conn)
    assert v_again == 3
    assert applied == ["v2", "v3"], "no re-application on re-run"
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
    # now-working v2 step should apply cleanly (v1 already applied,
    # user_version at 1).
    monkeypatch.undo()
    v = migrations.migrate(conn)
    assert v == 2, "a subsequent migrate() must apply the now-working step"
    conn.close()
