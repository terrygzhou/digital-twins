"""T003 (RED): mcp_authenticator — MCP credential gate (feature 004, R1).

Mirrors 003's ``_auth_checker`` credential order (cli.py) but returns
``(bool, email | None)`` instead of a bare ``bool`` / ``str`` so the MCP
executor (T010) can resolve the caller's role via ``accounts.get_role``.

Credential order (identical to 003):
  1. ``DT_SERVICE_TOKEN`` env + ``hmac.compare_digest`` → service account
     email (the ``mcp.service_account_email`` knob, default ``"system"``).
  2. ``verify_personal_token(db, token)`` → the token's account email.
  3. ``verify_session(db, token)`` → the session's account email.

Fail-closed semantics (mirroring 003's checker):
- missing / no ``Bearer`` prefix / unknown credential → ``(False, None)``
- a *known* credential whose account no longer exists → ``(False, reason)``
  where ``reason`` is a non-empty string naming the failure (403-class
  denial, not a 401-class unknown-credential denial).
"""

import os

import pytest

from digital_twins.accounts import create_account
from digital_twins.auth import create_personal_token, create_session
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) state DB with accounts / personal_tokens / sessions."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_service_env(monkeypatch):
    """Ensure DT_SERVICE_TOKEN is absent unless a test sets it explicitly."""
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# T003: mcp_authenticator returns a callable with the right contract
# ---------------------------------------------------------------------------

def test_returns_callable(db):
    """mcp_authenticator(db) returns a callable authenticate(headers)."""
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db)
    assert callable(auth)


def test_callable_returns_tuple(db):
    """authenticate(headers) always returns a 2-tuple (bool, email|None)."""
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db)
    # No Authorization header at all → missing credential → (False, None)
    result = auth({})
    assert isinstance(result, tuple)
    assert len(result) == 2
    ok, email = result
    assert ok is False
    assert email is None


# ---------------------------------------------------------------------------
# T003: valid personal token → (True, <email>)
# ---------------------------------------------------------------------------

def test_valid_personal_token(db):
    """A live personal token authenticates to its account's email."""
    from digital_twins.mcp.auth import mcp_authenticator
    create_account(db, "alice@example.com", "pw-alice")
    _tid, plaintext = create_personal_token(db, "alice@example.com")
    auth = mcp_authenticator(db)
    ok, email = auth(_bearer(plaintext))
    assert ok is True
    assert email == "alice@example.com"


# ---------------------------------------------------------------------------
# T003: valid session token → (True, <email>)
# ---------------------------------------------------------------------------

def test_valid_session_token(db):
    """A live session token authenticates to its account's email."""
    from digital_twins.mcp.auth import mcp_authenticator
    create_account(db, "bob@example.com", "pw-bob")
    plaintext, _expires = create_session(db, "bob@example.com")
    auth = mcp_authenticator(db)
    ok, email = auth(_bearer(plaintext))
    assert ok is True
    assert email == "bob@example.com"


# ---------------------------------------------------------------------------
# T003: valid DT_SERVICE_TOKEN → (True, mcp.service_account_email)
# ---------------------------------------------------------------------------

def test_service_token_resolves_to_default_email(db, monkeypatch):
    """DT_SERVICE_TOKEN match (constant-time) → the default service email."""
    from digital_twins.mcp.auth import mcp_authenticator
    monkeypatch.setenv("DT_SERVICE_TOKEN", "svc-secret-token")
    auth = mcp_authenticator(db)  # default service_account_email="system"
    ok, email = auth(_bearer("svc-secret-token"))
    assert ok is True
    assert email == "system"


def test_service_token_custom_email(db, monkeypatch):
    """DT_SERVICE_TOKEN with a custom service_account_email knob."""
    from digital_twins.mcp.auth import mcp_authenticator
    monkeypatch.setenv("DT_SERVICE_TOKEN", "svc-secret-token")
    auth = mcp_authenticator(db, service_account_email="svc@example.com")
    ok, email = auth(_bearer("svc-secret-token"))
    assert ok is True
    assert email == "svc@example.com"


def test_service_token_wrong_value_denied(db, monkeypatch):
    """A Bearer that does NOT match DT_SERVICE_TOKEN is not a service auth."""
    from digital_twins.mcp.auth import mcp_authenticator
    monkeypatch.setenv("DT_SERVICE_TOKEN", "the-real-secret")
    auth = mcp_authenticator(db)
    # "wrong-secret" != "the-real-secret" and matches no personal/session
    # token either → unknown credential → (False, None).
    ok, email = auth(_bearer("wrong-secret"))
    assert ok is False
    assert email is None


# ---------------------------------------------------------------------------
# T003: missing / unknown Bearer → (False, None)
# ---------------------------------------------------------------------------

def test_missing_authorization_header(db):
    """No Authorization header at all → (False, None)."""
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db)
    ok, email = auth({})
    assert ok is False
    assert email is None


def test_no_bearer_prefix(db):
    """Authorization without a 'Bearer ' prefix → (False, None)."""
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db)
    ok, email = auth({"Authorization": "Basic dXNlcjpwYXNz"})
    assert ok is False
    assert email is None


def test_unknown_bearer(db):
    """A Bearer that matches no credential → (False, None)."""
    from digital_twins.mcp.auth import mcp_authenticator
    auth = mcp_authenticator(db)
    ok, email = auth(_bearer("totally-unknown-credential"))
    assert ok is False
    assert email is None


# ---------------------------------------------------------------------------
# T003: fail closed — a credential whose account no longer exists is denied
# ---------------------------------------------------------------------------
#
# personal_tokens.account_email / sessions.account_email carry
# ``ON DELETE CASCADE`` (state.models v3, FKs ON in the connection). Deleting
# an account cascade-deletes its token rows, so after the delete the bearer
# is genuinely *unknown* → ``(False, None)`` (401-class), exactly as 003's
# read-only /status checker fails closed on any unknown credential. The
# ``(False, <reason>)`` 403-class branch is reserved for a *known* credential
# whose account is suspended (future mutating routes — 004/005), not for the
# cascade-deleted case. These tests pin that an account deletion never
# yields an ``(True, ...)`` grant (no orphan-credential grant, fail closed).

def test_personal_token_for_deleted_account_fails_closed(db):
    """Deleting an account cascade-deletes its personal token → no grant.

    Fail closed: after the delete the bearer is unknown → ``(False, None)``.
    The critical assertion is that it is NOT granted an
    ``(True, <email>)`` — an orphaned credential must not authenticate.
    """
    from digital_twins.mcp.auth import mcp_authenticator
    create_account(db, "ghost@example.com", "pw-ghost")
    _tid, plaintext = create_personal_token(db, "ghost@example.com")
    db.execute("DELETE FROM accounts WHERE email=?", ("ghost@example.com",))
    db.commit()
    auth = mcp_authenticator(db)
    ok, email_or_reason = auth(_bearer(plaintext))
    # No grant under any circumstance (fail closed).
    assert ok is False
    # Cascade-deleted → the token row is gone → unknown credential → None
    # (401-class), mirroring 003's /status behavior on an unknown bearer.
    assert email_or_reason is None


def test_session_token_for_deleted_account_fails_closed(db):
    """Deleting an account cascade-deletes its session → no grant.

    Same fail-closed contract as the personal-token case: the bearer is
    unknown after the delete → ``(False, None)``, never ``(True, ...)``.
    """
    from digital_twins.mcp.auth import mcp_authenticator
    create_account(db, "phantom@example.com", "pw-phantom")
    plaintext, _expires = create_session(db, "phantom@example.com")
    db.execute("DELETE FROM accounts WHERE email=?", ("phantom@example.com",))
    db.commit()
    auth = mcp_authenticator(db)
    ok, email_or_reason = auth(_bearer(plaintext))
    assert ok is False
    assert email_or_reason is None


def test_known_credential_dead_account_returns_reason_not_grant(db, monkeypatch):
    """The 403-class fail-closed branch: a *known* credential whose account
    no longer resolves to a role → ``(False, <reason>)``, never a grant.

    This exercises the ``get_role`` fail-closed branch in the authenticator
    (independent of the schema's ON DELETE CASCADE) by driving the
    underlying verifiers directly: a personal-token lookup that returns an
    email with no live account must be denied with a named reason, not
    granted. Patched at the unit boundary so the branch is asserted in
    isolation.
    """
    from digital_twins import accounts, auth as auth_mod
    from digital_twins.mcp.auth import mcp_authenticator

    # Drive the personal-token branch to an email whose get_role is None.
    # Patch the source modules (mcp.auth references them as _auth /
    # _accounts) so the unit boundary is exercised in isolation.
    monkeypatch.setattr(auth_mod, "verify_personal_token",
                        lambda db, token: "orphan@example.com")
    monkeypatch.setattr(auth_mod, "verify_session", lambda db, token: None)
    monkeypatch.setattr(accounts, "get_role", lambda db, email: None)
    auth = mcp_authenticator(db)
    ok, email_or_reason = auth(_bearer("whatever"))
    assert ok is False
    # A known-but-dead credential is denied with a reason (403-class),
    # not a bare unknown-credential None.
    assert isinstance(email_or_reason, str) and email_or_reason
