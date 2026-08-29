"""init first-admin step (T008, 003 C-4/R7).

Contract (specs/003-multi-user/contracts/cli.md, "digital-tokens init"):
after the state DB is migrated, ``init`` checks the ``accounts`` table.
Empty -> create the first account with ``role='admin'`` (credentials from
``INIT_ADMIN_EMAIL`` / ``INIT_ADMIN_PASSWORD`` env vars, or an interactive
prompt); echo "created first admin account ``<email>``".  Non-empty ->
create nothing; echo "admin already exists: ``<first-email>``" and skip
(idempotent: a re-run of ``init`` never creates a second admin).

Credential rule (auth-only, same as 002's ``DT_USER_PASSWORD``): read from
``os.environ`` directly, never a config knob, never in argv, never echoed
to stdout/stderr by the implementation.
"""

import pytest
from click.testing import CliRunner

from digital_twins import health
from digital_twins.cli import cli
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)  # keep .env resolution away from the repo
    return config_dir, state_dir


def _stub_health(monkeypatch, ok=True):
    def _run(cfg):
        return [health.HealthResult(ep, ok, "ok" if ok else "down", "")
                for ep in ("qdrant", "neo4j", "llm")]
    monkeypatch.setattr(health, "run_health_checks", _run)


def _run_init(runner, env, args=(), input=None):
    """Invoke ``init --yes`` with the given env additions; return result."""
    result = runner.invoke(cli, list(args) or ["init", "--yes"],
                           env=env or {}, input=input)
    return result


def _emails(state_dir):
    db = connect(state_dir)
    try:
        return [r[0] for r in db.execute(
            "SELECT email FROM accounts ORDER BY rowid")]
    finally:
        db.close()


# --- empty DB: first init creates an admin (C-4, R7) -----------------------


def test_init_empty_db_creates_admin_from_env(env_dirs, monkeypatch):
    """init on an empty accounts table creates the first account as admin,
    reading email + password from INIT_ADMIN_EMAIL / INIT_ADMIN_PASSWORD."""
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-secret-1")
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    # the first account exists, is the admin, and is the only account
    assert _emails(state_dir) == ["admin@example.com"]
    db = connect(state_dir)
    try:
        role = db.execute(
            "SELECT role FROM accounts WHERE email=?",
            ("admin@example.com",)).fetchone()[0]
        assert role == "admin"
        # the password was stored hashed, not in plaintext
        pw = db.execute(
            "SELECT password_hash FROM accounts WHERE email=?",
            ("admin@example.com",)).fetchone()[0]
        assert pw.startswith("pbkdf2$")
        assert "admin-secret-1" not in pw
    finally:
        db.close()


def test_init_echoes_created_first_admin(env_dirs, monkeypatch):
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-secret-1")
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert "created first admin account `admin@example.com`" in result.output


# --- idempotency: a re-run never creates a second admin (US1 S1) -----------


def test_init_rerun_does_not_create_second_admin(env_dirs, monkeypatch):
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-secret-1")
    runner = CliRunner()
    r1 = runner.invoke(cli, ["init", "--yes"])
    assert r1.exit_code == 0, r1.output
    assert _emails(state_dir) == ["admin@example.com"]
    # re-run: different email in the env must NOT create a second account
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "other@example.com")
    r2 = runner.invoke(cli, ["init", "--yes"])
    assert r2.exit_code == 0, r2.output
    assert _emails(state_dir) == ["admin@example.com"]
    assert "admin already exists: `admin@example.com`" in r2.output
    # and the second run did not echo a creation
    assert "created first admin account" not in r2.output


def test_init_rerun_no_env_still_idempotent(env_dirs, monkeypatch):
    """A re-run without the env vars (and not a TTY) still skips cleanly:
    the idempotent path is 'echo admin already exists + skip', not a crash."""
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    runner = CliRunner()
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-secret-1")
    r1 = runner.invoke(cli, ["init", "--yes"])
    assert r1.exit_code == 0, r1.output
    # clear the env vars: the table is non-empty, so no prompt is needed
    monkeypatch.delenv("INIT_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("INIT_ADMIN_PASSWORD", raising=False)
    r2 = runner.invoke(cli, ["init", "--yes"])
    assert r2.exit_code == 0, r2.output
    assert _emails(state_dir) == ["admin@example.com"]
    assert "admin already exists: `admin@example.com`" in r2.output


# --- credentials are env-only: never in argv, never in logs ----------------


def test_init_credentials_not_in_argparse_or_logs(env_dirs, monkeypatch):
    """The password must not appear in output (logs) and the command takes
    no argv credential flags at all."""
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "s3cr3t-pw-xyz")
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert "s3cr3t-pw-xyz" not in result.output
    # the init command exposes no credential flags (only --yes, 001/002)
    help_out = CliRunner().invoke(cli, ["init", "--help"])
    assert help_out.exit_code == 0, help_out.output
    assert "--password" not in help_out.output
    assert "--email" not in help_out.output
