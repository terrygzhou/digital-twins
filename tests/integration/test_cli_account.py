"""account list|set-role|delete|whoami commands (T011, 003 US2).

Contract (specs/003-multi-user/contracts/cli.md, "digital-tokens account"):
- ``account list``: admin-only. Lists email, role, created_at, last_active.
- ``account set-role --email E --role {admin|scheduler|reader}``: admin-only.
  Last-admin guard (SC-002): refuse with exit 1 if it would leave zero admins.
- ``account delete --email E``: admin-only. Same last-admin guard. Cascades
  to personal_tokens/sessions via FK ON DELETE CASCADE.
- ``account whoami``: authenticated, any role. Prints caller identity + role.
  The only non-admin-gated subcommand.

All authenticate first (DT_PERSONAL_TOKEN or DT_USER_PASSWORD), then check
R3 (account management: admin only, except whoami).

Red test first: these commands do not exist yet.
"""

import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect


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

    # Create a minimal kb.local.yml so load() works
    import yaml
    starter = {
        "state_dir": str(state_dir),
        "config_dir": str(config_dir),
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(starter), encoding="utf-8")

    # Migrate the state DB
    db = connect(state_dir)
    from digital_twins.state.migrations import migrate
    migrate(db)

    # Seed three accounts: admin, scheduler, reader
    _seed_account(db, "admin@example.com", "admin", "admin-pw")
    _seed_account(db, "sched@example.com", "scheduler", "sched-pw")
    _seed_account(db, "reader@example.com", "reader", "reader-pw")
    db.close()

    return config_dir, state_dir


def _make_token(state_dir, email):
    """Create a personal token for ``email`` and return the plaintext."""
    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, plaintext = create_personal_token(db, email)
    db.close()
    return plaintext


# ---------------------------------------------------------------------------
# account list
# ---------------------------------------------------------------------------


def test_account_list_admin(env_dirs):
    """An admin can list accounts; output shows email, role, created_at,
    last_active."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "list",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # All three seeded emails should appear
    assert "admin@example.com" in result.output
    assert "sched@example.com" in result.output
    assert "reader@example.com" in result.output
    # The role column header should appear
    assert "role" in result.output.lower()


def test_account_list_reader_denied(env_dirs):
    """A reader calling ``account list`` exits 2 with a named reason."""
    config_dir, state_dir = env_dirs
    reader_token = _make_token(state_dir, "reader@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "list",
    ], env={"DT_PERSONAL_TOKEN": reader_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "reader" in result.output.lower() or "role" in result.output.lower(), (
        f"error should name the role: {result.output}"
    )


def test_account_list_scheduler_denied(env_dirs):
    """A scheduler calling ``account list`` exits 2."""
    config_dir, state_dir = env_dirs
    sched_token = _make_token(state_dir, "sched@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "list",
    ], env={"DT_PERSONAL_TOKEN": sched_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_list_no_credentials_exit2(env_dirs, monkeypatch):
    """account list with no credentials exits 2."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "list",
    ])
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


# ---------------------------------------------------------------------------
# account set-role
# ---------------------------------------------------------------------------


def test_account_set_role_admin_to_scheduler(env_dirs):
    """An admin can set a reader to scheduler. The change persists."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "set-role",
        "--email", "reader@example.com",
        "--role", "scheduler",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # Verify the role change persisted
    db = connect(state_dir)
    from digital_twins.accounts import get_role
    role = get_role(db, "reader@example.com")
    db.close()
    assert role == "scheduler", (
        f"expected role 'scheduler', got {role!r}"
    )


def test_account_set_role_admin_to_admin_noop(env_dirs):
    """Setting a user's role to admin when they are already admin is a no-op
    but still exits 0."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "set-role",
        "--email", "admin@example.com",
        "--role", "admin",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_set_role_last_admin_demote_refused(env_dirs):
    """Demoting the last admin exits 1 with 'cannot demote the last admin'."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "set-role",
        "--email", "admin@example.com",
        "--role", "reader",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 1, (
        f"expected exit 1, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "last admin" in result.output.lower(), (
        f"error should mention last admin: {result.output}"
    )


def test_account_set_role_scheduler_denied(env_dirs):
    """A scheduler calling ``account set-role`` exits 2."""
    config_dir, state_dir = env_dirs
    sched_token = _make_token(state_dir, "sched@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "set-role",
        "--email", "reader@example.com",
        "--role", "admin",
    ], env={"DT_PERSONAL_TOKEN": sched_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_set_role_unknown_email_exit2(env_dirs):
    """Setting the role of a non-existent email exits 2."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "set-role",
        "--email", "ghost@example.com",
        "--role", "reader",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_set_role_last_admin_with_second_admin_ok(env_dirs):
    """With two admins, demoting one to reader succeeds (exit 0)."""
    config_dir, state_dir = env_dirs
    # Add a second admin so the first is no longer the last
    db = connect(state_dir)
    _seed_account(db, "admin2@example.com", "admin", "admin2-pw")
    db.close()

    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "set-role",
        "--email", "admin@example.com",
        "--role", "reader",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # Verify the demotion persisted
    db = connect(state_dir)
    from digital_twins.accounts import get_role
    role = get_role(db, "admin@example.com")
    db.close()
    assert role == "reader"


# ---------------------------------------------------------------------------
# account delete
# ---------------------------------------------------------------------------


def test_account_delete_admin(env_dirs):
    """An admin can delete a reader account. The account is gone."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "delete",
        "--email", "reader@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # Verify the account is gone
    db = connect(state_dir)
    from digital_twins.accounts import get_role
    role = get_role(db, "reader@example.com")
    db.close()
    assert role is None, (
        f"expected account to be deleted, got role {role!r}"
    )


def test_account_delete_last_admin_refused(env_dirs):
    """Deleting the last admin exits 1 with 'cannot delete the last admin'."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "delete",
        "--email", "admin@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 1, (
        f"expected exit 1, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "last admin" in result.output.lower(), (
        f"error should mention last admin: {result.output}"
    )


def test_account_delete_scheduler_denied(env_dirs):
    """A scheduler calling ``account delete`` exits 2."""
    config_dir, state_dir = env_dirs
    sched_token = _make_token(state_dir, "sched@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "delete",
        "--email", "reader@example.com",
    ], env={"DT_PERSONAL_TOKEN": sched_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_delete_unknown_email_exit2(env_dirs):
    """Deleting a non-existent email exits 2."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "delete",
        "--email", "ghost@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_delete_cascades_to_tokens(env_dirs):
    """Deleting an account cascades to their personal_tokens (FK ON DELETE
    CASCADE). The tokens become unusable."""
    config_dir, state_dir = env_dirs

    # Create a token for the reader before deletion
    reader_token = _make_token(state_dir, "reader@example.com")
    admin_token = _make_token(state_dir, "admin@example.com")

    # Before delete: the reader's token works
    db = connect(state_dir)
    from digital_twins.auth import verify_personal_token
    verified = verify_personal_token(db, reader_token)
    db.close()
    assert verified == "reader@example.com", (
        f"token should verify before delete, got {verified!r}"
    )

    # Admin deletes the reader
    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "delete",
        "--email", "reader@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # After delete: the reader's token no longer works
    db = connect(state_dir)
    verified = verify_personal_token(db, reader_token)
    db.close()
    assert verified is None, (
        "deleted account's token must not verify"
    )


# ---------------------------------------------------------------------------
# account whoami
# ---------------------------------------------------------------------------


def test_account_whoami_admin(env_dirs):
    """An admin can call whoami; output shows identity + role."""
    config_dir, state_dir = env_dirs
    admin_token = _make_token(state_dir, "admin@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "whoami",
    ], env={"DT_PERSONAL_TOKEN": admin_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "admin@example.com" in result.output
    assert "admin" in result.output.lower()


def test_account_whoami_reader(env_dirs):
    """A reader can call whoami (the only non-admin-gated subcommand)."""
    config_dir, state_dir = env_dirs
    reader_token = _make_token(state_dir, "reader@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "whoami",
    ], env={"DT_PERSONAL_TOKEN": reader_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "reader@example.com" in result.output
    assert "reader" in result.output.lower()


def test_account_whoami_scheduler(env_dirs):
    """A scheduler can call whoami."""
    config_dir, state_dir = env_dirs
    sched_token = _make_token(state_dir, "sched@example.com")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "whoami",
    ], env={"DT_PERSONAL_TOKEN": sched_token,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "sched@example.com" in result.output
    assert "scheduler" in result.output.lower()


def test_account_whoami_no_credentials_exit2(env_dirs, monkeypatch):
    """whoami with no credentials exits 2."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "whoami",
    ])
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_account_whoami_invalid_token_exit2(env_dirs):
    """whoami with an invalid DT_PERSONAL_TOKEN exits 2."""
    config_dir, state_dir = env_dirs

    runner = CliRunner()
    result = runner.invoke(cli, [
        "account", "whoami",
    ], env={"DT_PERSONAL_TOKEN": "deadbeef",
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
