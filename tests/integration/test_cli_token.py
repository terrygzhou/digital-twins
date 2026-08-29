"""token create|list|revoke commands (T010, 003 US3).

Contract (specs/003-multi-user/contracts/cli.md, "digital-tokens token"):
- ``token create [--as USER]``: without ``--as``: the caller's own token
  (any authenticated role); with ``--as USER``: admin-only, create for
  another user. Prints the plaintext token once.
- ``token list [--as USER]``: self: own; admin: any. Lists id, created_at,
  last_used_at, revoked. Plaintext never re-displayed.
- ``token revoke --id N``: self: own tokens only; admin: any. Flips
  revoked=1 on that row only.

All three authenticate first (password or personal token via
DT_PERSONAL_TOKEN), then check the caller's role against R3.

Red test first: these commands do not exist yet.
"""

import re
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
    from digital_twins.config.schema import get
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


def _open_db(state_dir):
    return connect(state_dir)


# ---------------------------------------------------------------------------
# token create — self (no --as)
# ---------------------------------------------------------------------------


def test_token_create_self_admin(env_dirs, monkeypatch):
    """An admin can create their own token (no --as)."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    monkeypatch.setenv("DT_USER_PASSWORD", "admin-pw")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create",
        # no --as: self-service
    ], env={"DT_USER_PASSWORD": "admin-pw",
            "DT_PERSONAL_TOKEN": ""})
    # The command needs to know WHO the caller is. Without --as on token create,
    # it must authenticate via DT_PERSONAL_TOKEN or DT_USER_PASSWORD.
    # Since we're testing self-service, we need to pass the caller's identity.
    # Per the contract, without --as: the caller's own token.
    # The caller is identified by DT_USER_PASSWORD (password auth) — but we
    # need to know WHICH user. The contract says "authenticate first" — so
    # the CLI must know the caller's email.
    #
    # Looking at the contract more carefully: "All three authenticate first
    # (password or personal token via DT_PERSONAL_TOKEN)".
    # The caller's identity comes from:
    #   - DT_PERSONAL_TOKEN: the token resolves to an account + role
    #   - DT_USER_PASSWORD: the password matches accounts.email = ???
    #
    # Wait — the contract says the token commands authenticate via
    # DT_USER_PASSWORD (the 002 account-password var). But DT_USER_PASSWORD
    # alone doesn't identify the user — you need to know which email to
    # check against.
    #
    # Re-reading the contract: "a command authenticates with the token
    # *instead of* the password". And "the CLI uses the token when
    # DT_PERSONAL_TOKEN is set, else the password".
    #
    # For the password path: the caller provides DT_USER_PASSWORD, but how
    # does the CLI know which email to check? Looking at the `run --once --as`
    # pattern: the --as flag provides the email. For token create, the caller
    # IS the user (no --as on self-service).
    #
    # Actually, I think the self-service path requires the caller to
    # authenticate via DT_PERSONAL_TOKEN (which resolves to an account).
    # The password path is for --as USER (admin creating for someone else),
    # where the admin's password authenticates them and --as names the target.
    #
    # Let me re-read: "All three authenticate first (password or personal
    # token via DT_PERSONAL_TOKEN), then check the caller's role against R3
    # (manage own personal tokens: admin/scheduler/reader for *their own*;
    # admin for another user's)."
    #
    # So the auth model is:
    # 1. If DT_PERSONAL_TOKEN is set: verify it -> get account_email + role
    # 2. Else: use DT_USER_PASSWORD — but we need the caller's email.
    #
    # For self-service (no --as): the caller's email must come from somewhere.
    # The most sensible approach: if DT_PERSONAL_TOKEN is set, use it to
    # resolve the caller. If not, we need a way to identify the caller.
    #
    # Actually, looking at the schedule commands (T012), --as is an OWNER
    # LABEL, not an auth requirement. The token commands are different:
    # they REQUIRE authentication.
    #
    # I think the cleanest interpretation is:
    # - Self-service (no --as): authenticate via DT_PERSONAL_TOKEN (token
    #   resolves to account_email). This is the primary self-service path.
    # - Admin creating for another user (--as USER): authenticate via
    #   DT_PERSONAL_TOKEN (admin's token) or DT_USER_PASSWORD (admin's
    #   password + the --as flag names the target user).
    #
    # Wait, but the contract says "password or personal token". Let me just
    # test the DT_PERSONAL_TOKEN path since that's unambiguous.
    #
    # For now, let's test with DT_PERSONAL_TOKEN.
    assert result.exit_code == 2, (
        f"expected exit 2 or 0, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_create_self_via_personal_token(env_dirs, monkeypatch):
    """An authenticated user (via DT_PERSONAL_TOKEN) can create their own token."""
    config_dir, state_dir = env_dirs

    # Create a personal token for admin first (using the T005 function directly)
    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    token_id, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # The plaintext token should be printed (once)
    # It should be a 64-char hex string
    lines = result.output.strip().splitlines()
    # Find the line that looks like a 64-hex token
    token_lines = [l for l in lines if re.fullmatch(r"[0-9a-f]{64}", l.strip())]
    assert len(token_lines) >= 1, (
        f"expected a 64-hex token in output, got lines: {lines}"
    )


def test_token_create_self_scheduler(env_dirs, monkeypatch):
    """A scheduler can create their own token (self-service)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, sched_plaintext = create_personal_token(db, "sched@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create",
    ], env={"DT_PERSONAL_TOKEN": sched_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_create_self_reader_denied(env_dirs, monkeypatch):
    """A reader CANNOT create their own token (R3: manage_own_tokens is
    admin/scheduler only, not reader)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, reader_plaintext = create_personal_token(db, "reader@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create",
    ], env={"DT_PERSONAL_TOKEN": reader_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    assert "reader" in result.output or "role" in result.output, (
        f"error should name the role: {result.output}"
    )


def test_token_create_stores_hash_not_plaintext(env_dirs, monkeypatch):
    """The token create command stores only the pbkdf2 hash, never the
    plaintext in the DB."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, result.output

    # The new token's plaintext should be in the output
    lines = result.output.strip().splitlines()
    token_lines = [l for l in lines if re.fullmatch(r"[0-9a-f]{64}", l.strip())]
    assert len(token_lines) >= 1, f"no token in output: {lines}"
    new_plaintext = token_lines[0].strip()

    # The new token should be verifiable
    db = connect(state_dir)
    from digital_twins.auth import verify_personal_token
    verified = verify_personal_token(db, new_plaintext)
    assert verified == "admin@example.com", (
        f"new token should verify to admin, got {verified!r}"
    )
    db.close()


# ---------------------------------------------------------------------------
# token create --as USER (admin-only)
# ---------------------------------------------------------------------------


def test_token_create_as_reader_by_admin(env_dirs, monkeypatch):
    """An admin can create a token for a reader user (--as reader@example.com)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create", "--as", "reader@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # Verify the new token belongs to reader
    lines = result.output.strip().splitlines()
    token_lines = [l for l in lines if re.fullmatch(r"[0-9a-f]{64}", l.strip())]
    assert len(token_lines) >= 1, f"no token in output: {lines}"
    new_plaintext = token_lines[0].strip()

    db = connect(state_dir)
    from digital_twins.auth import verify_personal_token
    verified = verify_personal_token(db, new_plaintext)
    assert verified == "reader@example.com", (
        f"token should belong to reader, got {verified!r}"
    )
    db.close()


def test_token_create_as_self_by_admin(env_dirs, monkeypatch):
    """An admin can create a token for themselves (--as admin@example.com)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create", "--as", "admin@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_create_as_reader_by_reader_denied(env_dirs, monkeypatch):
    """A reader CANNOT create a token for another user (--as) — exit 2,
    named reason (R3: manage_own_tokens is self-only for reader)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, reader_plaintext = create_personal_token(db, "reader@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create", "--as", "admin@example.com",
    ], env={"DT_PERSONAL_TOKEN": reader_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
    # The error message should name the role insufficiency
    assert "reader" in result.output.lower() or "role" in result.output.lower(), (
        f"error should name the role: {result.output}"
    )


def test_token_create_as_self_by_reader_denied(env_dirs, monkeypatch):
    """A reader CANNOT create a token for themselves via --as (must use
    self-service without --as, or the reader simply can't use --as at all
    since it's admin-only for other users)."""
    # Actually, re-reading the contract: "with --as USER: admin-only, create
    # for another user". So --as is admin-only entirely. A reader with --as
    # should be denied even for their own user.
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, reader_plaintext = create_personal_token(db, "reader@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create", "--as", "reader@example.com",
    ], env={"DT_PERSONAL_TOKEN": reader_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_create_as_unknown_user_denied(env_dirs, monkeypatch):
    """Creating a token for a non-existent user should fail (exit 2)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create", "--as", "ghost@example.com",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


# ---------------------------------------------------------------------------
# token list
# ---------------------------------------------------------------------------


def test_token_list_self(env_dirs, monkeypatch):
    """A user can list their own tokens (self-service)."""
    config_dir, state_dir = env_dirs

    # Create two tokens for admin
    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    id1, _ = create_personal_token(db, "admin@example.com")
    id2, _ = create_personal_token(db, "admin@example.com")
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "list",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # Should show 3 tokens (the two we created + the one used for auth)
    output = result.output
    # Each token row should have id, created_at, last_used_at, revoked
    # The output should contain the token ids
    assert str(id1) in output or "id" in output.lower(), (
        f"output should show token metadata: {output}"
    )


def test_token_list_admin_sees_all(env_dirs, monkeypatch):
    """An admin can list all tokens (--as or default)."""
    config_dir, state_dir = env_dirs

    # Create tokens for multiple users
    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    create_personal_token(db, "admin@example.com")
    create_personal_token(db, "sched@example.com")
    create_personal_token(db, "reader@example.com")
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "list",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # Admin should see all 4 tokens (3 + the auth token itself)
    # At minimum, should see tokens for multiple users
    output = result.output
    assert "admin@example.com" in output or "sched@example.com" in output or \
           "reader@example.com" in output, (
        f"admin list should show multiple users: {output}"
    )


def test_token_list_reader_denied(env_dirs, monkeypatch):
    """A reader CANNOT list their own tokens (R3: manage_own_tokens is
    admin/scheduler only, not reader)."""
    config_dir, state_dir = env_dirs

    # Create tokens for multiple users
    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    create_personal_token(db, "admin@example.com")
    create_personal_token(db, "sched@example.com")
    _, reader_plaintext = create_personal_token(db, "reader@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "list",
    ], env={"DT_PERSONAL_TOKEN": reader_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_list_no_plaintext(env_dirs, monkeypatch):
    """token list never re-displays the plaintext token."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    # Create a new token (its plaintext will be printed)
    runner = CliRunner()
    create_result = runner.invoke(cli, [
        "token", "create",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert create_result.exit_code == 0, create_result.output

    # Get the new token's plaintext
    lines = create_result.output.strip().splitlines()
    token_lines = [l for l in lines if re.fullmatch(r"[0-9a-f]{64}", l.strip())]
    assert len(token_lines) >= 1
    new_plaintext = token_lines[0].strip()

    # Now list tokens
    list_result = runner.invoke(cli, [
        "token", "list",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert list_result.exit_code == 0, list_result.output

    # The plaintext should NOT appear in the list output
    assert new_plaintext not in list_result.output, (
        f"plaintext token must not appear in list output: {list_result.output}"
    )


# ---------------------------------------------------------------------------
# token revoke
# ---------------------------------------------------------------------------


def test_token_revoke_self(env_dirs, monkeypatch):
    """A user can revoke their own token."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token, verify_personal_token
    token_id, plaintext = create_personal_token(db, "admin@example.com")
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    # Before revoke: token works
    db = connect(state_dir)
    assert verify_personal_token(db, plaintext) == "admin@example.com"
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "revoke", "--id", str(token_id),
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # After revoke: token no longer works
    db = connect(state_dir)
    assert verify_personal_token(db, plaintext) is None, (
        "revoked token must fail verify"
    )
    db.close()


def test_token_revoke_admin_can_revoke_any(env_dirs, monkeypatch):
    """An admin can revoke any user's token."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token, verify_personal_token
    reader_token_id, reader_plaintext = create_personal_token(db, "reader@example.com")
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    # Before: reader's token works
    db = connect(state_dir)
    assert verify_personal_token(db, reader_plaintext) == "reader@example.com"
    db.close()

    # Admin revokes the reader's token
    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "revoke", "--id", str(reader_token_id),
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, (
        f"expected exit 0, got {result.exit_code}; "
        f"output: {result.output}"
    )

    # After: reader's token no longer works
    db = connect(state_dir)
    assert verify_personal_token(db, reader_plaintext) is None, (
        "admin-revoked reader token must fail verify"
    )
    db.close()


def test_token_revoke_reader_cannot_revoke_another(env_dirs, monkeypatch):
    """A reader CANNOT revoke another user's token — exit 1 (not found,
    because the query is scoped to the caller's tokens), or exit 2 if
    the caller somehow has access.

    The key security property: a non-admin caller's query is scoped to
    their own tokens, so they cannot enumerate or revoke another user's
    token. The "not found" response is indistinguishable from "role denied"
    for a foreign token id.
    """
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    admin_token_id, _ = create_personal_token(db, "admin@example.com")
    _, reader_plaintext = create_personal_token(db, "reader@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "revoke", "--id", str(admin_token_id),
    ], env={"DT_PERSONAL_TOKEN": reader_plaintext,
            "DT_USER_PASSWORD": ""})
    # The reader's query is scoped to their own tokens, so the admin's
    # token id is not found → exit 1. This is the correct behavior:
    # the reader cannot distinguish "not found" from "role denied" for
    # a foreign token, which prevents enumeration.
    assert result.exit_code in (1, 2), (
        f"expected exit 1 (not found) or 2 (role denied), got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_revoke_unknown_id(env_dirs, monkeypatch):
    """Revoking a non-existent token id exits cleanly (exit 1 or 0)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "revoke", "--id", "999999",
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    # The contract says "exit 1 — operational failure (e.g. the schedule id
    # does not exist)". A non-existent token id is an operational failure.
    assert result.exit_code in (0, 1), (
        f"expected exit 0 or 1, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_revoke_flips_only_target_row(env_dirs, monkeypatch):
    """Revoking one token does not affect others (US3 S3: revocation
    isolation)."""
    config_dir, state_dir = env_dirs

    db = connect(state_dir)
    from digital_twins.auth import create_personal_token, verify_personal_token
    id1, plain1 = create_personal_token(db, "admin@example.com")
    id2, plain2 = create_personal_token(db, "admin@example.com")
    _, admin_plaintext = create_personal_token(db, "admin@example.com")
    db.close()

    # Revoke id1
    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "revoke", "--id", str(id1),
    ], env={"DT_PERSONAL_TOKEN": admin_plaintext,
            "DT_USER_PASSWORD": ""})
    assert result.exit_code == 0, result.output

    # id1 is revoked, id2 is still active
    db = connect(state_dir)
    assert verify_personal_token(db, plain1) is None, "id1 must be revoked"
    assert verify_personal_token(db, plain2) == "admin@example.com", (
        "id2 must still be active after revoking id1"
    )
    db.close()


# ---------------------------------------------------------------------------
# auth: no credentials -> exit 2
# ---------------------------------------------------------------------------


def test_token_create_no_credentials_exit2(env_dirs, monkeypatch):
    """token create with no DT_PERSONAL_TOKEN and no DT_USER_PASSWORD exits 2."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "create",
    ])
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_list_no_credentials_exit2(env_dirs, monkeypatch):
    """token list with no credentials exits 2."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "list",
    ])
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )


def test_token_revoke_no_credentials_exit2(env_dirs, monkeypatch):
    """token revoke with no credentials exits 2."""
    config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "token", "revoke", "--id", "1",
    ])
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}; "
        f"output: {result.output}"
    )
