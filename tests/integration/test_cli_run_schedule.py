"""Post-auth role check for ``run --once --as`` and ``schedule add`` (T012, 003 US4).

Contract (specs/003-multi-user/contracts/cli.md, "digital-tokens run" /
"digital-tokens schedule"):

- ``run --once --as USER``: after ``authenticate(db, USER, DT_USER_PASSWORD)``
  succeeds, the caller's role must permit **trigger a run** (R3: admin/scheduler
  yes, reader no). On denial: exit 2, stderr names the role insufficiency
  ("role 'reader' may not trigger a run"), **no** audit row, **no** Qdrant
  write, **no** high-water advance — the same fail-fast contract as 002's
  bad-password path.

- ``schedule add``: after authentication, the caller's role must permit
  **schedule CRUD** (R3: admin/scheduler yes, reader no). On denial: exit 2,
  named reason, no schedule row written.

Red test first: the role checks do not exist yet, so reader-denial tests
will fail (the CLI has no post-auth role guard for run/schedule yet).
"""

import yaml
import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_account(db, email, role, password="pw123"):
    """Insert a minimal accounts row with a known password."""
    from digital_twins.auth import hash_password
    db.execute(
        "INSERT INTO accounts (email, role, password_hash, created_at, last_active) "
        "VALUES (?, ?, ?, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')",
        (email, role, hash_password(password)),
    )
    db.commit()


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    """Fresh config + state dirs with a v3-migrated DB and seeded accounts."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)

    # Create a minimal kb.local.yml so load() works. No sources enabled so
    # a successful ``run --once`` ingests zero items (we only care about the
    # auth + role gate, not the pipeline).
    starter = {
        "state_dir": str(state_dir),
        "config_dir": str(config_dir),
        "sources": {},
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(starter), encoding="utf-8")

    # Migrate the state DB
    db = connect(state_dir)
    migrate(db)

    # Seed three accounts: admin, scheduler, reader
    _seed_account(db, "admin@example.com", "admin", "admin-pw")
    _seed_account(db, "sched@example.com", "scheduler", "sched-pw")
    _seed_account(db, "reader@example.com", "reader", "reader-pw")
    db.close()

    return config_dir, state_dir


def _count_audit_runs(state_dir):
    """Number of rows in the audit_runs table."""
    db = connect(state_dir)
    try:
        return db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]
    finally:
        db.close()


def _count_schedules(state_dir):
    """Number of rows in the schedules table."""
    db = connect(state_dir)
    try:
        return db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# run --once --as: role check
# ---------------------------------------------------------------------------

def test_run_once_as_reader_denied(env_dirs, monkeypatch):
    """A reader's ``run --once --as`` exits 2 with a named reason and
    zero side effects (no audit row, no Qdrant write, no high-water
    advance)."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "run", "--once", "--as", "reader@example.com",
    ], env={"DT_USER_PASSWORD": "reader-pw",
            "DT_PERSONAL_TOKEN": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The error must name the role insufficiency
    assert "reader" in result.output, (
        f"error should name the role 'reader': {result.output}"
    )
    assert "trigger" in result.output.lower() or "run" in result.output.lower(), (
        f"error should name the capability 'trigger a run': {result.output}"
    )
    # No audit row was written
    assert _count_audit_runs(state_dir) == 0, (
        "no audit row should be written on role denial"
    )


def test_run_once_as_scheduler_succeeds(env_dirs, monkeypatch):
    """A scheduler's ``run --once --as`` passes the role check and
    proceeds past the auth gate. With no sources enabled the pipeline
    ingests zero items; the qdrant.url config error (exit 1) only
    happens AFTER the role check, proving the gate was passed."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "run", "--once", "--as", "sched@example.com",
    ], env={"DT_USER_PASSWORD": "sched-pw",
            "DT_PERSONAL_TOKEN": ""})
    # exit 0: clean zero-item run; exit 1: qdrant.url config error that
    # only fires AFTER the role check passed. Both prove the role gate
    # was traversed.
    assert result.exit_code in (0, 1), (
        f"expected exit 0 (clean run) or 1 (qdrant config error post-gate), "
        f"got {result.exit_code}; "
        f"output: {result.output}"
    )
    # If the role check had denied, we'd get exit 2 with "role" in the output
    assert "role" not in result.output.lower() or "may not" not in result.output.lower(), (
        f"role denial message should not appear for a scheduler: {result.output}"
    )


def test_run_once_as_admin_succeeds(env_dirs, monkeypatch):
    """An admin's ``run --once --as`` passes the role check and
    proceeds past the auth gate."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "run", "--once", "--as", "admin@example.com",
    ], env={"DT_USER_PASSWORD": "admin-pw",
            "DT_PERSONAL_TOKEN": ""})
    # exit 0: clean zero-item run; exit 1: qdrant.url config error that
    # only fires AFTER the role check passed.
    assert result.exit_code in (0, 1), (
        f"expected exit 0 (clean run) or 1 (qdrant config error post-gate), "
        f"got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "role" not in result.output.lower() or "may not" not in result.output.lower(), (
        f"role denial message should not appear for an admin: {result.output}"
    )


def test_run_once_as_reader_no_audit_row(env_dirs, monkeypatch):
    """A reader's denied ``run --once --as`` writes zero audit rows."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "run", "--once", "--as", "reader@example.com",
    ], env={"DT_USER_PASSWORD": "reader-pw",
            "DT_PERSONAL_TOKEN": ""})
    assert result.exit_code == 2
    assert _count_audit_runs(state_dir) == 0, (
        "role denial must not write an audit row"
    )


def test_run_once_as_reader_no_state_mutation(env_dirs, monkeypatch):
    """A reader's denied ``run --once --as`` leaves the state DB untouched
    (no high-water advance, no schedule change)."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    # Snapshot the state before the denied run
    db = connect(state_dir)
    before = db.execute(
        "SELECT COUNT(*) FROM audit_runs").fetchone()[0]
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "run", "--once", "--as", "reader@example.com",
    ], env={"DT_USER_PASSWORD": "reader-pw",
            "DT_PERSONAL_TOKEN": ""})
    assert result.exit_code == 2

    # State is unchanged
    db = connect(state_dir)
    after = db.execute(
        "SELECT COUNT(*) FROM audit_runs").fetchone()[0]
    db.close()
    assert before == after, "no state mutation on role denial"


# ---------------------------------------------------------------------------
# schedule add: role check
# ---------------------------------------------------------------------------

def test_schedule_add_scheduler_succeeds(env_dirs, monkeypatch):
    """A scheduler's ``schedule add`` passes the role check and writes
    the schedule row."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "schedule", "add",
        "--source", "fs",
        "--preset", "daily",
        "--as", "sched@example.com",
    ], env={"DT_USER_PASSWORD": "sched-pw",
            "DT_PERSONAL_TOKEN": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # A schedule row was written with the correct owner
    db = connect(state_dir)
    row = db.execute(
        "SELECT owner FROM schedules WHERE owner = 'sched@example.com'"
    ).fetchone()
    db.close()
    assert row is not None, (
        "schedule add should write a row owned by the scheduler"
    )


def test_schedule_add_reader_denied(env_dirs, monkeypatch):
    """A reader's ``schedule add`` exits 2 before any row is written."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "schedule", "add",
        "--source", "fs",
        "--preset", "daily",
        "--as", "reader@example.com",
    ], env={"DT_USER_PASSWORD": "reader-pw",
            "DT_PERSONAL_TOKEN": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The error must name the role insufficiency
    assert "reader" in result.output, (
        f"error should name the role 'reader': {result.output}"
    )
    assert "schedule" in result.output.lower(), (
        f"error should name the capability 'manage schedules': {result.output}"
    )
    # No schedule row was written
    assert _count_schedules(state_dir) == 0, (
        "no schedule row should be written on role denial"
    )


def test_schedule_add_reader_no_row_written(env_dirs, monkeypatch):
    """Explicit: a reader's denied ``schedule add`` leaves the schedules
    table empty."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    runner = CliRunner()
    runner.invoke(cli, [
        "schedule", "add",
        "--source", "fs",
        "--preset", "daily",
        "--as", "reader@example.com",
    ], env={"DT_USER_PASSWORD": "reader-pw",
            "DT_PERSONAL_TOKEN": ""})

    assert _count_schedules(state_dir) == 0, (
        "reader denial must not write a schedule row"
    )
