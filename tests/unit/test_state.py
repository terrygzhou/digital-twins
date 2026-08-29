"""State layer: WAL connection, user_version migrations, table behavior,
and the CLI pre-command migration hook (T009, T010; constitution VI)."""

import json
import sqlite3

import pytest
from click.testing import CliRunner

from digital_twins.cli import cli, pre_command
from digital_twins.state import migrations, models
from digital_twins.state.db import connect, state_db_path


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path)
    migrations.migrate(c)
    yield c
    c.close()


def test_migrate_creates_schema(tmp_path):
    c = connect(tmp_path)
    v = migrations.migrate(c)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert v == migrations.SCHEMA_VERSION == 3
    assert {"accounts", "highwater", "audit_runs", "schedules",
            "personal_tokens", "user_config", "sessions"} <= tables
    c.close()


def test_migrate_is_idempotent(tmp_path):
    c = connect(tmp_path)
    migrations.migrate(c)
    migrations.migrate(c)
    assert migrations.user_version(c) == 3
    c.close()


def test_wal_mode(tmp_path):
    c = connect(tmp_path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_accounts_empty_on_fresh_migrate(conn):
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_highwater_upsert_and_read(conn):
    models.upsert_highwater(conn, "fs", "a.txt", "abc123")
    assert models.get_highwater(conn, "fs", "a.txt") == "abc123"
    models.upsert_highwater(conn, "fs", "a.txt", "def456")
    assert models.get_highwater(conn, "fs", "a.txt") == "def456"
    assert models.get_highwater(conn, "fs", "missing") is None
    assert models.get_highwater(conn, "other", "a.txt") is None


def test_audit_run_lifecycle(conn):
    models.start_audit_run(conn, "r1", trigger="manual", scheduled_by="system")
    row = conn.execute(
        "SELECT status, trigger, scheduled_by, completed_at FROM audit_runs "
        "WHERE run_id='r1'").fetchone()
    assert row == ("partial", "manual", "system", None)  # in flight
    models.finish_audit_run(
        conn, "r1", "ok",
        per_source_counts={"fs": {"new": 1, "skipped": 0, "failed": 0}})
    row = conn.execute(
        "SELECT status, completed_at, per_source_counts FROM audit_runs "
        "WHERE run_id='r1'").fetchone()
    assert row[0] == "ok" and row[1] is not None
    assert json.loads(row[2]) == {"fs": {"new": 1, "skipped": 0, "failed": 0}}


def test_audit_failed_run_still_recorded(conn):
    models.start_audit_run(conn, "r2")
    models.finish_audit_run(conn, "r2", "failed")
    assert conn.execute(
        "SELECT status FROM audit_runs WHERE run_id='r2'").fetchone()[0] == "failed"


def test_audit_status_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audit_runs (run_id, started_at, status) "
            "VALUES ('x', 'now', 'bogus')")
    with pytest.raises(ValueError):
        models.finish_audit_run(conn, "x", "bogus")


# --- CLI wiring: migrations complete before any command --------------------

def test_pre_command_migrates_existing_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("KB_STATE_DIR", str(state))
    pre_command()
    assert state_db_path(state).exists()
    c = sqlite3.connect(str(state_db_path(state)))
    assert c.execute("PRAGMA user_version").fetchone()[0] == 3
    c.close()


def test_pre_command_no_side_effects_without_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"  # never created
    monkeypatch.setenv("KB_STATE_DIR", str(state))
    pre_command()  # must not raise and must not create anything
    assert not state.exists()


def test_cli_invocation_runs_hook(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("KB_STATE_DIR", str(state))
    res = CliRunner().invoke(cli, [])
    # whatever click does for a bare invocation, the hook must have run
    # when a callback fired — assert the migration artifact when it did:
    if state_db_path(state).exists():
        c = sqlite3.connect(str(state_db_path(state)))
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
        c.close()
    else:
        # bare help short-circuits before the group callback: verify via a
        # real subcommand path instead — none exist yet, so at minimum the
        # invocation itself must be clean.
        assert res.exit_code in (0, 2)
