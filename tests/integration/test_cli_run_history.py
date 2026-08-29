"""run-history CLI command: view own/all run history (US2 S4, US3 S2, R3).

Contract (specs/003-multi-user/spec.md US2 S4, US3 S2):
- ``run-history --as USER``: after auth, the caller's role must permit
  **view own history** (R3: all roles).  Returns only runs where
  ``scheduled_by = caller_email``.  ``system``-attributed runs are excluded.
- ``run-history --as ADMIN --all``: admin-only (view_all_history).  Returns
  every audit row.

Red test first: the ``run-history`` CLI command does not exist yet.
"""

import yaml
import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


def _seed_account(db, email, role, password="pw123"):
    from digital_twins.auth import hash_password
    db.execute(
        "INSERT INTO accounts (email, role, password_hash, created_at, last_active) "
        "VALUES (?, ?, ?, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')",
        (email, role, hash_password(password)),
    )
    db.commit()


def _seed_audit_run(db, run_id, scheduled_by, status="ok"):
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, trigger, "
        "scheduled_by, per_source_counts) VALUES (?, '2025-01-01', '2025-01-01', ?, 'manual', ?, '{}')",
        (run_id, status, scheduled_by),
    )
    db.commit()


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kb.yml").write_text(
        yaml.safe_dump({"sources": {}, "qdrant": {"url": "http://localhost:6333"}}),
        encoding="utf-8")
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump({"state_dir": str(state_dir)}), encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("DT_USER_PASSWORD", "pw123")
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    return tmp_path


def _open_db(tmp_path):
    db = connect(tmp_path / "state")
    migrate(db)
    return db


class TestRunHistoryOwn:
    """run-history without --all: view own runs only."""

    def test_sees_own_runs(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        _seed_audit_run(db, "run-1", "alice@example.com")
        _seed_audit_run(db, "run-2", "alice@example.com")
        _seed_audit_run(db, "run-3", "bob@example.com")
        _seed_audit_run(db, "run-sys", "system")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, ["run-history", "--as", "alice@example.com"])
        assert result.exit_code == 0, result.output
        assert "run-1" in result.output
        assert "run-2" in result.output
        assert "run-3" not in result.output  # bob's run, not alice's
        assert "run-sys" not in result.output  # system run excluded

    def test_no_runs_shows_message(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "reader")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, ["run-history", "--as", "alice@example.com"])
        assert result.exit_code == 0, result.output
        assert "no runs" in result.output

    def test_reader_can_view_own_history(self, env_dirs):
        """Reader has view_own_history in R3."""
        db = _open_db(env_dirs)
        _seed_account(db, "reader1@example.com", "reader")
        _seed_audit_run(db, "r-run-1", "reader1@example.com")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, ["run-history", "--as", "reader1@example.com"])
        assert result.exit_code == 0, result.output
        assert "r-run-1" in result.output


class TestRunHistoryAll:
    """run-history --all: admin-only, view every user's runs."""

    def test_admin_sees_all(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "admin@example.com", "admin")
        _seed_audit_run(db, "run-a", "alice@example.com")
        _seed_audit_run(db, "run-b", "bob@example.com")
        _seed_audit_run(db, "run-sys", "system")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, ["run-history", "--as", "admin@example.com", "--all"])
        assert result.exit_code == 0, result.output
        assert "run-a" in result.output
        assert "run-b" in result.output
        assert "run-sys" in result.output  # admin sees system runs too

    def test_scheduler_denied(self, env_dirs):
        """Scheduler lacks view_all_history (R3: admin only)."""
        db = _open_db(env_dirs)
        _seed_account(db, "sched@example.com", "scheduler")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, ["run-history", "--as", "sched@example.com", "--all"])
        assert result.exit_code == 2, (
            f"scheduler should be denied --all, got exit {result.exit_code}: {result.output}")

    def test_reader_denied(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "reader@example.com", "reader")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, ["run-history", "--as", "reader@example.com", "--all"])
        assert result.exit_code == 2, result.output


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
