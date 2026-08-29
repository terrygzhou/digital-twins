"""config set|list|unset commands (T018, 003 US3 — per-user overrides, R5).

Contract (specs/003-multi-user/contracts/cli.md, "Per-user config command (R5)"):

- ``config set --as USER --source S --key K --value V``: set a per-user
  override. The role must permit **manage own personal config**
  (admin/scheduler for *their own* user; admin for another user's).
  ``K`` is restricted to the overridable keys (``enabled``, ``max_items``,
  ``timeout_s``); other keys → exit 2 ("not a user-overridable knob").
  ``V`` is type-coerced via 001 ``schema.coerce`` at write time
  (fail-fast on a malformed value).
- ``config list --as USER``: list a user's overrides.
- ``config unset --as USER --source S --key K``: remove one.

All three authenticate first (DT_PERSONAL_TOKEN or DT_USER_PASSWORD + --as),
then check the caller's role against R3.

Red test first: these commands do not exist yet.
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


def _create_token(db, email):
    """Create a personal token for ``email`` and return the plaintext."""
    from digital_twins.auth import create_personal_token
    _, plaintext = create_personal_token(db, email)
    return plaintext


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

    starter = {
        "state_dir": str(state_dir),
        "config_dir": str(config_dir),
        "sources": {},
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(starter), encoding="utf-8")

    db = connect(state_dir)
    migrate(db)

    _seed_account(db, "admin@example.com", "admin", "admin-pw")
    _seed_account(db, "sched@example.com", "scheduler", "sched-pw")
    _seed_account(db, "reader@example.com", "reader", "reader-pw")

    # Pre-create personal tokens for each account (used in token-auth tests)
    admin_token = _create_token(db, "admin@example.com")
    sched_token = _create_token(db, "sched@example.com")
    reader_token = _create_token(db, "reader@example.com")

    db.close()

    return {
        "config_dir": config_dir,
        "state_dir": state_dir,
        "admin_token": admin_token,
        "sched_token": sched_token,
        "reader_token": reader_token,
    }


def _count_user_config_rows(state_dir, email=None):
    """Number of rows in user_config, optionally filtered by account_email."""
    db = connect(state_dir)
    try:
        if email is None:
            return db.execute("SELECT COUNT(*) FROM user_config").fetchone()[0]
        return db.execute(
            "SELECT COUNT(*) FROM user_config WHERE account_email=?",
            (email,),
        ).fetchone()[0]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# config set — scheduler sets their own config (success path)
# ---------------------------------------------------------------------------

def test_config_set_scheduler_own_success(env_dirs, monkeypatch):
    """A scheduler sets their own max_items=50 on hermes → success (exit 0)."""
    state_dir = env_dirs["state_dir"]
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The override was written to user_config
    assert _count_user_config_rows(state_dir, "sched@example.com") == 1, (
        "a user_config row should exist for the scheduler"
    )


def test_config_set_admin_own_success(env_dirs, monkeypatch):
    """An admin sets their own max_items=100 on pi → success (exit 0)."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "admin@example.com",
        "--source", "pi",
        "--key", "max_items",
        "--value", "100",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["admin_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "admin@example.com") == 1


def test_config_set_reader_own_denied(env_dirs, monkeypatch):
    """A reader cannot set their own config → exit 2.

    R3: manage_own_config is available to admin and scheduler only,
    not reader. Reader is pure query (no mutating capabilities).
    """
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "reader@example.com",
        "--source", "hermes",
        "--key", "timeout_s",
        "--value", "60",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["reader_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "reader@example.com") == 0


# ---------------------------------------------------------------------------
# config set — reader tries to set another user's config (denied)
# ---------------------------------------------------------------------------

def test_config_set_reader_another_user_denied(env_dirs, monkeypatch):
    """A reader tries to set another user's config → exit 2 with a
    'role `reader` may not manage config for another user' style message."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",  # target: the scheduler
        "--source", "hermes",
        "--key", "max_items",
        "--value", "99",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["reader_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The error must name the role and the denial
    assert "reader" in result.output, (
        f"error should name the role 'reader': {result.output}"
    )
    # No row was written for the scheduler
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0, (
        "denied config set must not write a row"
    )


def test_config_set_scheduler_another_user_denied(env_dirs, monkeypatch):
    """A scheduler tries to set another user's config → exit 2."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "admin@example.com",  # target: the admin
        "--source", "hermes",
        "--key", "max_items",
        "--value", "99",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "scheduler" in result.output, (
        f"error should name the role 'scheduler': {result.output}"
    )
    assert _count_user_config_rows(state_dir, "admin@example.com") == 0


# ---------------------------------------------------------------------------
# config set — admin CAN set another user's config (success path)
# ---------------------------------------------------------------------------

def test_config_set_admin_another_user_success(env_dirs, monkeypatch):
    """An admin sets another user's config → success (exit 0)."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",  # target: the scheduler
        "--source", "hermes",
        "--key", "max_items",
        "--value", "75",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["admin_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "sched@example.com") == 1


# ---------------------------------------------------------------------------
# config set — non-overridable key (denied)
# ---------------------------------------------------------------------------

def test_config_set_non_overridable_key_denied(env_dirs, monkeypatch):
    """Setting a non-overridable key (e.g. `prefix`) → exit 2 with
    'not a user-overridable knob'."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "prefix",
        "--value", "custom",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "not a user-overridable knob" in result.output, (
        f"error should say 'not a user-overridable knob': {result.output}"
    )
    # No row was written
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0


# ---------------------------------------------------------------------------
# config set — malformed value (coercion error)
# ---------------------------------------------------------------------------

def test_config_set_malformed_value_denied(env_dirs, monkeypatch):
    """A malformed value (e.g. max_items=abc) → exit 2 with a coercion
    error message."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "abc",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The error should mention coercion or type (the value is malformed)
    assert "abc" in result.output or "coerce" in result.output.lower() \
        or "invalid" in result.output.lower() or "type" in result.output.lower(), (
        f"error should mention the malformed value or coercion: {result.output}"
    )
    # No row was written
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0


def test_config_set_malformed_bool_denied(env_dirs, monkeypatch):
    """A malformed boolean (e.g. enabled=notabool) → exit 2."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "enabled",
        "--value", "notabool",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0


# ---------------------------------------------------------------------------
# config set — authentication failure
# ---------------------------------------------------------------------------

def test_config_set_no_credentials_denied(env_dirs, monkeypatch):
    """No credentials (no DT_PERSONAL_TOKEN, no DT_USER_PASSWORD) → exit 2."""
    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": "", "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_config_set_bad_password_denied(env_dirs, monkeypatch):
    """Wrong password → exit 2."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": "", "DT_USER_PASSWORD": "wrong-password"})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0


def test_config_set_bad_token_denied(env_dirs, monkeypatch):
    """Invalid DT_PERSONAL_TOKEN → exit 2."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": "deadbeef", "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0


# ---------------------------------------------------------------------------
# config list
# ---------------------------------------------------------------------------

def test_config_list_empty(env_dirs, monkeypatch):
    """config list with no overrides → 'no overrides' (or similar) + exit 0."""
    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "list",
        "--as", "sched@example.com",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "no overrides" in result.output, (
        f"should say 'no overrides' when empty: {result.output}"
    )


def test_config_list_shows_overrides(env_dirs, monkeypatch):
    """After setting overrides, config list shows them."""
    state_dir = env_dirs["state_dir"]

    # Set two overrides first
    runner = CliRunner()
    runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "pi",
        "--key", "timeout_s",
        "--value", "120",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})

    result = runner.invoke(cli, [
        "config", "list",
        "--as", "sched@example.com",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "hermes" in result.output, (
        f"should show 'hermes' source: {result.output}"
    )
    assert "max_items" in result.output, (
        f"should show 'max_items' key: {result.output}"
    )
    assert "pi" in result.output, (
        f"should show 'pi' source: {result.output}"
    )
    assert "timeout_s" in result.output, (
        f"should show 'timeout_s' key: {result.output}"
    )


def test_config_list_admin_sees_another_user(env_dirs, monkeypatch):
    """Admin can list another user's overrides via --as."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    # Admin sets an override for the scheduler
    runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "42",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["admin_token"],
            "DT_USER_PASSWORD": ""})

    # Admin lists the scheduler's overrides
    result = runner.invoke(cli, [
        "config", "list",
        "--as", "sched@example.com",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["admin_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "hermes" in result.output
    assert "max_items" in result.output


# ---------------------------------------------------------------------------
# config unset
# ---------------------------------------------------------------------------

def test_config_unset_removes_override(env_dirs, monkeypatch):
    """config unset removes a specific override."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    # Set an override
    runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert _count_user_config_rows(state_dir, "sched@example.com") == 1

    # Unset it
    result = runner.invoke(cli, [
        "config", "unset",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0


def test_config_unset_reader_another_user_denied(env_dirs, monkeypatch):
    """A reader cannot unset another user's override → exit 2."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    # Scheduler sets an override for themselves
    runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})

    # Reader tries to unset the scheduler's override
    result = runner.invoke(cli, [
        "config", "unset",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["reader_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The row is still there
    assert _count_user_config_rows(state_dir, "sched@example.com") == 1


def test_config_unset_non_overridable_key_denied(env_dirs, monkeypatch):
    """Unsetting a non-overridable key → exit 2."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "config", "unset",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "prefix",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["sched_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "not a user-overridable knob" in result.output, (
        f"error should say 'not a user-overridable knob': {result.output}"
    )


def test_config_unset_admin_another_user_success(env_dirs, monkeypatch):
    """Admin can unset another user's override."""
    state_dir = env_dirs["state_dir"]

    runner = CliRunner()
    # Admin sets an override for the scheduler
    runner.invoke(cli, [
        "config", "set",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
        "--value", "50",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["admin_token"],
            "DT_USER_PASSWORD": ""})
    assert _count_user_config_rows(state_dir, "sched@example.com") == 1

    # Admin unsets the scheduler's override
    result = runner.invoke(cli, [
        "config", "unset",
        "--as", "sched@example.com",
        "--source", "hermes",
        "--key", "max_items",
    ], env={"DT_PERSONAL_TOKEN": env_dirs["admin_token"],
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert _count_user_config_rows(state_dir, "sched@example.com") == 0
