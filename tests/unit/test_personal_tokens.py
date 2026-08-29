"""003 multi-user: personal_tokens CRUD (T005).

Contracts (specs/003-multi-user/data-model.md, research.md R2):
- ``create_personal_token(db, account_email) -> (id, plaintext)``:
  generates a 32-byte ``os.urandom(32).hex()`` token, stores only the
  pbkdf2 hash (R2: same scheme as ``hash_password``), returns the
  plaintext once. The DB must never contain the plaintext.
- ``verify_personal_token(db, plaintext) -> account_email | None``:
  re-derives the pbkdf2 hash, constant-time compares with
  ``hmac.compare_digest``, sets ``last_used_at`` on success, returns
  the account email; returns None for revoked or unknown tokens.
- ``list_personal_tokens(db, account_email=None)``: returns token
  metadata; with an account_email filter, only that account's rows.
- ``revoke_personal_token(db, token_id)``: flips ``revoked=1`` on
  that row only; the revoked token then fails ``verify``.

Red-first: these tests target functions that do not yet exist.
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
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
# create_personal_token
# ---------------------------------------------------------------------------

def test_create_token_returns_id_and_plaintext(tmp_path):
    """create_personal_token returns (int, str); the plaintext is 64 hex
    chars (32 bytes of os.urandom, R2)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token
        result = create_personal_token(db, "alice@example.com")
        assert isinstance(result, tuple), f"expected tuple, got {type(result)}"
        assert len(result) == 2, f"expected (id, plaintext), got {result!r}"
        token_id, plaintext = result
        assert isinstance(token_id, int), f"id must be int, got {type(token_id)}"
        assert isinstance(plaintext, str), f"plaintext must be str"
        # 32 bytes -> 64 hex chars (R2)
        assert re.fullmatch(r"[0-9a-f]{64}", plaintext), (
            f"plaintext must be 64 lowercase hex chars, got {plaintext!r}"
        )
    finally:
        db.close()


def test_create_token_stores_hash_not_plaintext(tmp_path):
    """The DB stores only the pbkdf2 hash, never the plaintext (R2 / data-model C-1).
    The token_hash column must be in ``pbkdf2$<salt_hex>$<hash_hex>`` format,
    and the plaintext must not appear anywhere in the DB file."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "bob@example.com")
        from digital_twins.auth import create_personal_token
        token_id, plaintext = create_personal_token(db, "bob@example.com")

        row = db.execute(
            "SELECT token_hash FROM personal_tokens WHERE id=?",
            (token_id,),
        ).fetchone()
        assert row is not None, "token row must exist in personal_tokens"
        stored = row[0]
        # pbkdf2$<32 hex>$<64 hex>
        assert stored.startswith("pbkdf2$"), f"hash must start with pbkdf2$, got {stored!r}"
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


def test_create_token_for_two_accounts_independent(tmp_path):
    """Two tokens for two different accounts are independent: each verifies
    to its own account."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        _seed_account(db, "bob@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token
        id_a, plain_a = create_personal_token(db, "alice@example.com")
        id_b, plain_b = create_personal_token(db, "bob@example.com")
        assert plain_a != plain_b, "two tokens must differ"
        assert verify_personal_token(db, plain_a) == "alice@example.com"
        assert verify_personal_token(db, plain_b) == "bob@example.com"
    finally:
        db.close()


def test_create_token_two_for_one_account(tmp_path):
    """An account can hold multiple tokens; both verify independently
    (US3 S3: revocation isolation)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token
        id_1, plain_1 = create_personal_token(db, "alice@example.com")
        id_2, plain_2 = create_personal_token(db, "alice@example.com")
        assert id_1 != id_2, "each token must get a distinct id"
        assert verify_personal_token(db, plain_1) == "alice@example.com"
        assert verify_personal_token(db, plain_2) == "alice@example.com"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# verify_personal_token
# ---------------------------------------------------------------------------

def test_verify_token_success_sets_last_used_at(tmp_path):
    """On a successful verify, last_used_at is set to a non-empty ISO-8601
    timestamp (data-model: 'set on a successful verify; NULL until first use')."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token
        token_id, plaintext = create_personal_token(db, "alice@example.com")

        # before verify: last_used_at is NULL
        before = db.execute(
            "SELECT last_used_at FROM personal_tokens WHERE id=?", (token_id,)
        ).fetchone()[0]
        assert before is None, f"last_used_at must be NULL before first verify, got {before!r}"

        result = verify_personal_token(db, plaintext)
        assert result == "alice@example.com"

        after = db.execute(
            "SELECT last_used_at FROM personal_tokens WHERE id=?", (token_id,)
        ).fetchone()[0]
        assert after is not None, "last_used_at must be set after verify"
        assert after != "", f"last_used_at must not be empty, got {after!r}"
        # must be parseable as ISO-8601
        datetime.fromisoformat(after)
    finally:
        db.close()


def test_verify_unknown_token_returns_none(tmp_path):
    """A token that was never created must return None, not raise."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import verify_personal_token
        result = verify_personal_token(db, "nonexistent-token-000")
        assert result is None, f"unknown token must return None, got {result!r}"
    finally:
        db.close()


def test_verify_revoked_token_returns_none(tmp_path):
    """After revocation, verify returns None for the revoked token (US3 S3)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token
        token_id, plaintext = create_personal_token(db, "alice@example.com")
        # verify works before revoke
        assert verify_personal_token(db, plaintext) == "alice@example.com"
        # revoke
        from digital_twins.auth import revoke_personal_token
        revoke_personal_token(db, token_id)
        # verify must now fail
        assert verify_personal_token(db, plaintext) is None, (
            "revoked token must return None"
        )
    finally:
        db.close()


def test_verify_plaintext_never_in_db(tmp_path):
    """After create + verify, the plaintext token must not appear in the
    DB file (R2: DB leak does not yield live credentials)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token
        token_id, plaintext = create_personal_token(db, "alice@example.com")
        verify_personal_token(db, plaintext)  # exercise the verify path
        db_path = Path(str(db.execute("PRAGMA database_list").fetchone()[2]))
        db_bytes = db_path.read_bytes()
        assert plaintext.encode("utf-8") not in db_bytes, (
            "plaintext must not leak into the DB after verify"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# list_personal_tokens
# ---------------------------------------------------------------------------

def test_list_tokens_with_account_filter(tmp_path):
    """list_personal_tokens(db, account_email) returns only that account's
    tokens; a different account's tokens are excluded."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        _seed_account(db, "bob@example.com")
        from digital_twins.auth import create_personal_token, list_personal_tokens
        create_personal_token(db, "alice@example.com")
        create_personal_token(db, "alice@example.com")
        create_personal_token(db, "bob@example.com")

        alice_tokens = list_personal_tokens(db, "alice@example.com")
        bob_tokens = list_personal_tokens(db, "bob@example.com")

        assert len(alice_tokens) == 2, f"expected 2 for alice, got {len(alice_tokens)}"
        assert len(bob_tokens) == 1, f"expected 1 for bob, got {len(bob_tokens)}"
        # every returned row must belong to the requested account
        for row in alice_tokens:
            assert row["account_email"] == "alice@example.com"
        for row in bob_tokens:
            assert row["account_email"] == "bob@example.com"
    finally:
        db.close()


def test_list_tokens_without_filter(tmp_path):
    """list_personal_tokens(db) with no account_email returns all tokens
    across all accounts."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        _seed_account(db, "bob@example.com")
        from digital_twins.auth import create_personal_token, list_personal_tokens
        create_personal_token(db, "alice@example.com")
        create_personal_token(db, "alice@example.com")
        create_personal_token(db, "bob@example.com")

        all_tokens = list_personal_tokens(db)
        assert len(all_tokens) == 3, f"expected 3 total, got {len(all_tokens)}"
        emails = {row["account_email"] for row in all_tokens}
        assert emails == {"alice@example.com", "bob@example.com"}
    finally:
        db.close()


def test_list_tokens_returns_metadata_not_hash(tmp_path):
    """list_personal_tokens must not expose the token_hash to the caller
    (defense in depth: the list endpoint is lower-privilege than the
    token store). Each row must carry the account_email and the id."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, list_personal_tokens
        token_id, _ = create_personal_token(db, "alice@example.com")
        rows = list_personal_tokens(db, "alice@example.com")
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == token_id
        assert row["account_email"] == "alice@example.com"
        # the hash must NOT be in the returned dict
        assert "token_hash" not in row, (
            f"list must not expose token_hash, got keys: {list(row.keys())}"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# revoke_personal_token
# ---------------------------------------------------------------------------

def test_revoke_flips_only_target_row(tmp_path):
    """revoking one token must not affect the other (US3 S3: revocation
    isolation)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token, \
            revoke_personal_token
        id_1, plain_1 = create_personal_token(db, "alice@example.com")
        id_2, plain_2 = create_personal_token(db, "alice@example.com")
        assert id_1 != id_2

        revoke_personal_token(db, id_1)

        # the revoked token fails, the other still works
        assert verify_personal_token(db, plain_1) is None, "revoked token must fail"
        assert verify_personal_token(db, plain_2) == "alice@example.com", (
            "sibling token must still verify after revoking a different one"
        )
        # DB state: id_1 revoked=1, id_2 revoked=0
        row1 = db.execute(
            "SELECT revoked FROM personal_tokens WHERE id=?", (id_1,)
        ).fetchone()
        row2 = db.execute(
            "SELECT revoked FROM personal_tokens WHERE id=?", (id_2,)
        ).fetchone()
        assert row1[0] == 1, "id_1 must be revoked"
        assert row2[0] == 0, "id_2 must still be active"
    finally:
        db.close()


def test_revoke_unknown_id_is_noop(tmp_path):
    """revoking a non-existent token id must not raise (defensive: the
    id may be stale)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import revoke_personal_token
        # no rows in personal_tokens at all; must not raise
        revoke_personal_token(db, 999999)
    finally:
        db.close()


def test_verify_token_plaintext_format(tmp_path):
    """verify_personal_token returns the account email (str), not the
    token id or hash — the caller uses it to stamp the owner tag."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token, verify_personal_token
        _id, plaintext = create_personal_token(db, "alice@example.com")
        result = verify_personal_token(db, plaintext)
        assert result == "alice@example.com", (
            f"verify must return the account email, got {result!r}"
        )
    finally:
        db.close()


def test_two_tokens_same_plaintext_collision(tmp_path):
    """Two calls to create_personal_token for the same account must never
    return the same plaintext (32-byte entropy makes this astronomically
    unlikely, but the UNIQUE constraint on token_hash guards it)."""
    db = _state_db(tmp_path)
    try:
        _seed_account(db, "alice@example.com")
        from digital_twins.auth import create_personal_token
        _, p1 = create_personal_token(db, "alice@example.com")
        _, p2 = create_personal_token(db, "alice@example.com")
        assert p1 != p2, "two 32-byte tokens must differ"
    finally:
        db.close()
