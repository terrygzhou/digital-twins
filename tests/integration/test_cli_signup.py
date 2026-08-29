"""signup command (T009, 003 C-4/R7).

Contract (specs/003-multi-user/contracts/cli.md, "digital-tokens signup"):
``signup --email E --password P`` creates an account.  Role is resolved by
the shared helper: first row in ``accounts`` -> ``admin``, else ``reader``
(C-4/R7).  Duplicate email -> exit 2, "account already exists: ``<email>``",
no second row.  The password is hashed with 001's ``hash_password`` (R1).
``created_at``/``last_active`` set to now.
"""

import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)
    return config_dir, state_dir


def _role_of(state_dir, email):
    db = connect(state_dir)
    try:
        row = db.execute(
            "SELECT role FROM accounts WHERE email=?", (email,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def _count(state_dir):
    db = connect(state_dir)
    try:
        return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        db.close()


def _pw_hash(state_dir, email):
    db = connect(state_dir)
    try:
        return db.execute(
            "SELECT password_hash FROM accounts WHERE email=?",
            (email,)).fetchone()[0]
    finally:
        db.close()


# --- first signup on a fresh DB is admin (C-4, R7) --------------------------


def test_signup_first_is_admin(env_dirs):
    """The first ``signup`` on a fresh (empty) DB creates an admin."""
    config_dir, state_dir = env_dirs
    runner = CliRunner()
    result = runner.invoke(cli, [
        "signup", "--email", "first@example.com",
        "--password", "first-pw-123"])
    assert result.exit_code == 0, result.output
    assert _count(state_dir) == 1
    assert _role_of(state_dir, "first@example.com") == "admin"


# --- second signup is reader (C-4, R7) --------------------------------------


def test_signup_second_is_reader(env_dirs):
    """The second ``signup`` (accounts non-empty) creates a reader."""
    config_dir, state_dir = env_dirs
    runner = CliRunner()
    r1 = runner.invoke(cli, [
        "signup", "--email", "first@example.com",
        "--password", "first-pw-123"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli, [
        "signup", "--email", "second@example.com",
        "--password", "second-pw-456"])
    assert r2.exit_code == 0, r2.output
    assert _count(state_dir) == 2
    assert _role_of(state_dir, "first@example.com") == "admin"
    assert _role_of(state_dir, "second@example.com") == "reader"


# --- duplicate email -> exit 2, no second row (C-4) -------------------------


def test_signup_duplicate_email_exit2_no_second_row(env_dirs):
    """A duplicate ``signup`` exits 2 with the named error and leaves no
    second row."""
    config_dir, state_dir = env_dirs
    runner = CliRunner()
    r1 = runner.invoke(cli, [
        "signup", "--email", "dup@example.com",
        "--password", "first-pw-123"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli, [
        "signup", "--email", "dup@example.com",
        "--password", "other-pw-456"])
    assert r2.exit_code == 2, r2.output
    assert "account already exists: `dup@example.com`" in r2.output
    assert _count(state_dir) == 1


# --- password is hashed, never plaintext (R1) --------------------------------


def test_signup_password_hashed_not_plaintext(env_dirs):
    """The password is stored hashed (pbkdf2), never in plaintext."""
    config_dir, state_dir = env_dirs
    runner = CliRunner()
    result = runner.invoke(cli, [
        "signup", "--email", "hash@example.com",
        "--password", "hunter2-secret"])
    assert result.exit_code == 0, result.output
    pw = _pw_hash(state_dir, "hash@example.com")
    assert pw.startswith("pbkdf2$")
    assert "hunter2-secret" not in pw


# --- created_at / last_active are set (C-4) ----------------------------------


def test_signup_sets_created_at_and_last_active(env_dirs):
    """``created_at`` and ``last_active`` are set to a non-empty timestamp."""
    config_dir, state_dir = env_dirs
    runner = CliRunner()
    result = runner.invoke(cli, [
        "signup", "--email", "ts@example.com",
        "--password", "ts-pw-123"])
    assert result.exit_code == 0, result.output
    db = connect(state_dir)
    try:
        row = db.execute(
            "SELECT created_at, last_active FROM accounts "
            "WHERE email=?", ("ts@example.com",)).fetchone()
        assert row is not None
        assert row[0]  # created_at non-empty
        assert row[1]  # last_active non-empty
    finally:
        db.close()
