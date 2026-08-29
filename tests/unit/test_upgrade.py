"""Upgrade preservation: seed state, run a version-bumped migration,
assert state + accounts + config survive (S7, NFR-15, SC-006, FR-012).

Constitution VI: migration completes before any new code runs.
"""

import json

from digital_twins.state import migrations, models
from digital_twins.state.db import connect


def _apply_v2_noop(conn):
    """No-op v2 migration: schema unchanged, data must survive."""
    pass


def _apply_v2_marker_table(conn):
    """v2 migration that adds a new table (trivial schema change)."""
    conn.execute("CREATE TABLE IF NOT EXISTS v2_marker (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO v2_marker (id) VALUES (1)")


def _apply_v3_noop(conn):
    """No-op v3 migration: data must survive."""
    pass


def _snapshot_all(conn):
    """Capture every row in every v1 table as a dict-of-lists."""
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
    }


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

def test_seed_data_survives_version_bumped_migration(tmp_path, monkeypatch):
    """All v1 rows are intact after a v1→v2 upgrade (S7, NFR-15)."""
    conn = connect(tmp_path)
    migrations.migrate(conn)  # apply v1
    _seed_v1(conn)
    before = _snapshot_all(conn)

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
        (2, _apply_v2_noop),
    ])
    v = migrations.migrate(conn)
    assert v == 2, "user_version must advance to 2"

    after = _snapshot_all(conn)
    for table in ("accounts", "highwater", "audit_runs"):
        assert after[table] == before[table], (
            f"{table} rows changed after v2 migration"
        )
    conn.close()


# 2 ------------------------------------------------------------------------

def test_accounts_survive_upgrade(tmp_path, monkeypatch):
    """Account rows survive a v1→v2 upgrade with all fields intact."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    _seed_v1(conn)

    accounts_before = conn.execute(
        "SELECT email, role, password_hash FROM accounts ORDER BY email"
    ).fetchall()
    assert len(accounts_before) == 2, "precondition: two accounts seeded"

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
        (2, _apply_v2_noop),
    ])
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

def test_highwater_survives_upgrade(tmp_path, monkeypatch):
    """High-water marks are unchanged after a v1→v2 upgrade (NFR-9)."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    _seed_v1(conn)

    hw_before = conn.execute(
        "SELECT source, item_key, last_key FROM highwater "
        "ORDER BY source, item_key"
    ).fetchall()
    assert len(hw_before) == 3, "precondition: three highwater rows seeded"

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
        (2, _apply_v2_noop),
    ])
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

def test_audit_runs_survive_upgrade(tmp_path, monkeypatch):
    """Audit run rows are unchanged after a v1→v2 upgrade (Constitution V)."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    _seed_v1(conn)

    runs_before = conn.execute(
        "SELECT run_id, started_at, status, trigger, scheduled_by, "
        "completed_at, per_source_counts FROM audit_runs ORDER BY run_id"
    ).fetchall()
    assert len(runs_before) == 2, "precondition: two audit runs seeded"

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
        (2, _apply_v2_noop),
    ])
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
    assert v1 == v2 == v3 == 1
    assert migrations.user_version(conn) == 1
    conn.close()


def test_migration_is_idempotent_after_upgrade(tmp_path, monkeypatch):
    """After a v1→v2 upgrade, re-running migrate() is a no-op."""
    conn = connect(tmp_path)
    migrations.migrate(conn)
    _seed_v1(conn)

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
        (2, _apply_v2_noop),
    ])
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
    migrations.migrate(conn)
    _seed_v1(conn)

    applied = []

    def apply_v2_track(conn_):
        applied.append("v2")
        _apply_v2_marker_table(conn_)

    def apply_v3_track(conn_):
        applied.append("v3")
        _apply_v3_noop(conn_)

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
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

def test_config_file_survives_upgrade(tmp_path, monkeypatch):
    """kb.local.yml on disk is untouched by the migration process (FR-012)."""
    config = tmp_path / "kb.local.yml"
    config.write_text("state_dir: /tmp/kb-state\nembedding_dim: 384\n")
    original = config.read_text()

    conn = connect(tmp_path)
    migrations.migrate(conn)
    _seed_v1(conn)

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, models.apply_v1),
        (2, _apply_v2_noop),
    ])
    migrations.migrate(conn)

    assert config.read_text() == original, "config file must not be modified"
    assert config.exists(), "config file must not be deleted"
    conn.close()
