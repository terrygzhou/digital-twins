"""003 multi-user: sessions CRUD (T006).

Contracts (specs/003-multi-user/data-model.md, research.md R4):
- ``create_session(db, account_email) -> (plaintext, expires_at)``:
  generates a 32-byte ``os.urandom(32).hex()`` token (64 hex chars,
  256-bit entropy), stores only the pbkdf2 hash in
  ``sessions.session_token``, and returns the plaintext once.
  ``expires_at`` is ``created_at + SESSION_TTL_HOURS`` (default 8 h) —
  a module constant, not a config knob (A2: the UI is minimal, so the
  TTL is fixed).
- ``verify_session(db, plaintext) -> account_email | None``:
  re-derives the pbkdf2 hash, constant-time compares with
  ``hmac.compare_digest``; returns the account email on a live,
  un-revoked session; returns None when ``expires_at < now``
  (expired) or ``revoked = 1`` (revoked) or the token is unknown.
- ``revoke_session(db, session_token)``:
  flips ``revoked=1`` on the matching row (stale/unknown tokens are
  no-ops, matching ``revoke_personal_token`` semantics).
- The 8 h TTL is a module-level constant (``SESSION_TTL_HOURS = 8``),
  not a config knob (R4 / data-model).

Red-first: these tests target functions that do not yet exist.
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _state_db(tmp_path):
    """Create a fresh v3-migrated state DB in tmp_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    migrate(db)
    return db


def _seed_account(db, email: str, password: str = "s3cretpw") -> None:
    """Insert a minimal accounts row (required for the FK)."""
    from digital_twins.auth import hash_password
    db.execute(
        "INSERT INTO accounts (email, role, password_hash) VALUES (?, ?, ?)",
        (email, "reader", hash_password(password)),
    )
    db.commit()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------

def test_create_session_returns_plaintext_and_expires_at(tmp_path):
    """create_session returns a 2-tuple ``(plaintext, expires_at)`` where
    plaintext is a 64-char lowercase hex string (32 bytes of os.urandom, R4)
    and expires_at is a parseable UTC ISO-8601 timestamp."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_session
        result = create_session(db, "alice@example.com")
        assert isinstance(result, tuple), f"expected tuple, got {type(result)}"
        assert len(result) == 2, f"expected (plaintext, expires_at), got {result!r}"
        plaintext, expires_at = result
        assert isinstance(plaintext, str), f"plaintext must be str"
        assert re.fullmatch(r"[0-9a-f]{64}", plaintext), (
            f"plaintext must be 64 lowercase hex chars, got {plaintext!r}"
        )
        assert isinstance(expires_at, str), f"expires_at must be str"
        # must parse as ISO-8601
        parsed = datetime.fromisoformat(expires_at)
        assert parsed.tzinfo is not None, "expires_at must be tz-aware"
    finally:
        db.close()


def test_create_session_expires_at_uses_8h_ttl(tmp_path):
    """expires_at must be exactly ``created_at + SESSION_TTL_HOURS`` where
    SESSION_TTL_HOURS is the module-level constant 8 (not a config knob)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        import digital_twins.auth as auth_mod
        from digital_twins.auth import create_session
        # the 8 h TTL must be a module constant, not a config knob
        assert auth_mod.SESSION_TTL_HOURS == 8, (
            f"SESSION_TTL_HOURS must be 8, got {auth_mod.SESSION_TTL_HOURS!r}"
        )
        # capture the before/after window
        before = datetime.now(timezone.utc)
        plaintext, expires_at = create_session(db, "alice@example.com")
        after = datetime.now(timezone.utc)

        parsed_expires = datetime.fromisoformat(expires_at)
        # created_at is truncated to whole seconds (timespec="seconds"),
        # so created_at <= before. Hence:
        #   expires_at = created_at + 8h <= before + 8h
        #   expires_at = created_at + 8h >= (before truncated to 1s) + 8h
        #            >= before + 8h - 1s
        # Use a 1s grace on the lower bound to absorb the truncation.
        expected_hi = before + timedelta(hours=auth_mod.SESSION_TTL_HOURS)
        expected_lo = (
            before
            + timedelta(hours=auth_mod.SESSION_TTL_HOURS)
            - timedelta(seconds=1)
        )
        assert expected_lo <= parsed_expires <= expected_hi, (
            f"expires_at {parsed_expires} must be created_at + "
            f"{auth_mod.SESSION_TTL_HOURS}h, got {parsed_expires}"
        )
    finally:
        db.close()


def test_create_session_stores_hash_not_plaintext(tmp_path):
    """The DB stores only the pbkdf2 hash of the plaintext (R4 / data-model).
    The ``sessions.session_token`` column must be in
    ``pbkdf2$<salt_hex>$<hash_hex>`` format, and the plaintext must not
    appear anywhere in the DB file."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "bob@example.com")
        from digital_twins.auth import create_session
        plaintext, _ = create_session(db, "bob@example.com")

        rows = db.execute(
            "SELECT session_token, account_email, created_at, expires_at, revoked "
            "FROM sessions"
        ).fetchall()
        assert len(rows) == 1, f"expected 1 session row, got {len(rows)}"
        stored, acct, created, expires, revoked = rows[0]
        assert acct == "bob@example.com"
        assert revoked == 0, "new session must start un-revoked"
        # pbkdf2$<32 hex>$<64 hex>
        assert stored.startswith("pbkdf2$"), (
            f"hash must start with pbkdf2$, got {stored!r}"
        )
        parts = stored.split("$")
        assert len(parts) == 3, f"expected 3 parts, got {parts!r}"
        assert len(parts[1]) == 32, "salt must be 16 bytes (32 hex chars)"
        assert len(parts[2]) == 64, "sha256 digest is 32 bytes (64 hex chars)"

        # The plaintext must NOT be in the DB file.
        db_path = Path(str(db.execute("PRAGMA database_list").fetchone()[2]))
        db_bytes = db_path.read_bytes()
        assert plaintext.encode("utf-8") not in db_bytes, (
            "plaintext token must not be stored in the DB"
        )
    finally:
        db.close()


def test_create_session_two_for_one_account_independent(tmp_path):
    """An account can hold multiple sessions; each verifies to its own
    account_email independently."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_session, verify_session
        p1, _ = create_session(db, "alice@example.com")
        p2, _ = create_session(db, "alice@example.com")
        assert p1 != p2, "two session tokens must differ"
        assert verify_session(db, p1) == "alice@example.com"
        assert verify_session(db, p2) == "alice@example.com"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# verify_session
# ---------------------------------------------------------------------------

def test_verify_session_success_returns_email(tmp_path):
    """A freshly created session verifies to its account_email (str)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_session, verify_session
        plaintext, _ = create_session(db, "alice@example.com")
        result = verify_session(db, plaintext)
        assert result == "alice@example.com", (
            f"verify must return the account email, got {result!r}"
        )
    finally:
        db.close()


def test_verify_session_unknown_token_returns_none(tmp_path):
    """A token that was never created must return None, not raise."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import verify_session
        result = verify_session(db, "nonexistent-session-token-000")
        assert result is None, f"unknown session must return None, got {result!r}"
    finally:
        db.close()


def test_verify_session_expired_returns_none(tmp_path):
    """Once ``expires_at < now``, verify_session returns None (R4:
    short-lived). The test advances the clock by directly updating the
    row's ``expires_at`` to a past timestamp."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_session, verify_session
        plaintext, _ = create_session(db, "alice@example.com")

        # sanity: a fresh session verifies
        assert verify_session(db, plaintext) == "alice@example.com"

        # advance the clock: set expires_at to 1s in the past
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="seconds")
        db.execute(
            "UPDATE sessions SET expires_at=? WHERE account_email=?",
            (past, "alice@example.com"),
        )
        db.commit()

        assert verify_session(db, plaintext) is None, (
            "expired session must return None"
        )
    finally:
        db.close()


def test_verify_session_revoked_returns_none(tmp_path):
    """After ``revoke_session``, verify_session returns None for the
    revoked token (R4: revocable)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import (
            create_session, verify_session, revoke_session,
        )
        plaintext, _ = create_session(db, "alice@example.com")
        # verify works before revoke
        assert verify_session(db, plaintext) == "alice@example.com"
        # revoke by plaintext token
        revoke_session(db, plaintext)
        # verify must now fail
        assert verify_session(db, plaintext) is None, (
            "revoked session must return None"
        )
    finally:
        db.close()


def test_verify_session_two_sessions_revocation_isolation(tmp_path):
    """Revoking one session must not affect a sibling session (R4: each
    row is independent)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import (
            create_session, verify_session, revoke_session,
        )
        p1, _ = create_session(db, "alice@example.com")
        p2, _ = create_session(db, "alice@example.com")
        assert p1 != p2

        revoke_session(db, p1)

        assert verify_session(db, p1) is None, "revoked session must fail"
        assert verify_session(db, p2) == "alice@example.com", (
            "sibling session must still verify after revoking a different one"
        )
    finally:
        db.close()


def test_verify_session_plaintext_never_in_db(tmp_path):
    """After create + verify, the plaintext session token must not appear
    in the DB file (R4: DB leak does not yield live credentials)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_session, verify_session
        plaintext, _ = create_session(db, "alice@example.com")
        verify_session(db, plaintext)  # exercise the verify path
        db_path = Path(str(db.execute("PRAGMA database_list").fetchone()[2]))
        db_bytes = db_path.read_bytes()
        assert plaintext.encode("utf-8") not in db_bytes, (
            "plaintext must not leak into the DB after verify"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# revoke_session
# ---------------------------------------------------------------------------

def test_revoke_session_flips_only_target_row(tmp_path):
    """revoking one session must not affect the other (R4: per-row
    revocation, mirroring personal-token isolation)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import (
            create_session, verify_session, revoke_session,
        )
        p1, _ = create_session(db, "alice@example.com")
        p2, _ = create_session(db, "alice@example.com")

        revoke_session(db, p1)

        # the revoked session fails, the other still works
        assert verify_session(db, p1) is None, "revoked session must fail"
        assert verify_session(db, p2) == "alice@example.com", (
            "sibling session must still verify after revoking a different one"
        )
    finally:
        db.close()


def test_revoke_session_unknown_token_is_noop(tmp_path):
    """revoking a non-existent session token must not raise (defensive:
    stale token from a previously-revoked or deleted row)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import revoke_session
        # no rows in sessions at all; must not raise
        revoke_session(db, "never-created-session-000")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 8 h TTL is a constant, not a knob
# ---------------------------------------------------------------------------

def test_session_ttl_is_module_constant_not_config_knob(tmp_path):
    """SESSION_TTL_HOURS must be a module-level constant equal to 8,
    and the TTL must not be exposed as a config knob (A2: the UI is
    minimal — the TTL is fixed)."""
    db = _state_db(tmp_path)
    try:
        import digital_twins.auth as auth_mod
        # the constant must exist and be 8
        assert hasattr(auth_mod, "SESSION_TTL_HOURS"), (
            "auth_mod must expose SESSION_TTL_HOURS"
        )
        assert auth_mod.SESSION_TTL_HOURS == 8, (
            f"SESSION_TTL_HOURS must be 8, got {auth_mod.SESSION_TTL_HOURS!r}"
        )
        # and it must be an int, not a float or a callable
        assert isinstance(auth_mod.SESSION_TTL_HOURS, int), (
            f"SESSION_TTL_HOURS must be int, got {type(auth_mod.SESSION_TTL_HOURS)}"
        )
    finally:
        db.close()
