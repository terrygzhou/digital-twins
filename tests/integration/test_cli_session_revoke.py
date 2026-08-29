"""session revoke CLI: invalidate a live web session (US3 S3, R4).

US3 S3: "revoking one does not affect the other."  A leaked session token
must be revocable; otherwise its only mitigation is the 8-hour TTL.  R4:
sessions are "short-lived and revocable."

Red test first: the ``session`` CLI group does not exist yet.
"""

import yaml
import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.auth import create_session, verify_session


def _seed_account(db, email, role, password="pw123"):
    from digital_twins.auth import hash_password
    db.execute(
        "INSERT INTO accounts (email, role, password_hash, created_at, last_active) "
        "VALUES (?, ?, ?, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')",
        (email, role, hash_password(password)),
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


class TestSessionRevoke:
    """session revoke flips revoked=1 on a live session."""

    def test_owner_revokes_own_session(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        plaintext, _expires = create_session(db, "alice@example.com")
        # Session is live before revoke
        assert verify_session(db, plaintext) == "alice@example.com"
        db.close()

        runner = CliRunner()
        with runner.isolation() as _:
            import os
            os.environ["DT_SESSION_TOKEN"] = plaintext
            result = runner.invoke(
                cli, ["session", "revoke", "--as", "alice@example.com"])
        assert result.exit_code == 0, result.output

        # Re-open DB and verify the session is now revoked
        db2 = _open_db(env_dirs)
        assert verify_session(db2, plaintext) is None, (
            "session still live after revoke")
        db2.close()

    def test_revoked_session_no_longer_verifies(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "bob@example.com", "reader")
        plaintext, _ = create_session(db, "bob@example.com")
        db.close()

        runner = CliRunner()
        import os
        os.environ["DT_SESSION_TOKEN"] = plaintext
        result = runner.invoke(cli, ["session", "revoke", "--as", "bob@example.com"])
        assert result.exit_code == 0, result.output

        db2 = _open_db(env_dirs)
        # Revoked sessions must not authenticate
        assert verify_session(db2, plaintext) is None
        db2.close()

    def test_unknown_token_denied(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        db.close()

        runner = CliRunner()
        import os
        os.environ["DT_SESSION_TOKEN"] = "not-a-real-session-token"
        result = runner.invoke(cli, ["session", "revoke", "--as", "alice@example.com"])
        assert result.exit_code == 2, (
            f"unknown token should exit 2, got {result.exit_code}: {result.output}")

    def test_admin_revokes_another_users_session(self, env_dirs):
        db = _open_db(env_dirs)
        _seed_account(db, "admin@example.com", "admin")
        _seed_account(db, "user1@example.com", "reader")
        plaintext, _ = create_session(db, "user1@example.com")
        db.close()

        runner = CliRunner()
        import os
        os.environ["DT_SESSION_TOKEN"] = plaintext
        # admin revokes user1's session
        result = runner.invoke(cli, ["session", "revoke", "--as", "admin@example.com"])
        assert result.exit_code == 0, result.output

        db2 = _open_db(env_dirs)
        assert verify_session(db2, plaintext) is None
        db2.close()

    def test_scheduler_cannot_revoke_another_users_session(self, env_dirs):
        """A non-admin cannot revoke another user's session."""
        db = _open_db(env_dirs)
        _seed_account(db, "sched@example.com", "scheduler")
        _seed_account(db, "user2@example.com", "reader")
        plaintext, _ = create_session(db, "user2@example.com")
        db.close()

        runner = CliRunner()
        import os
        os.environ["DT_SESSION_TOKEN"] = plaintext
        result = runner.invoke(cli, ["session", "revoke", "--as", "sched@example.com"])
        assert result.exit_code == 2, (
            f"scheduler cannot revoke another user's session, got "
            f"{result.exit_code}: {result.output}")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
