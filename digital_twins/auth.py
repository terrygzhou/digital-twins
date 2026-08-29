"""Password auth against the accounts store (002 ruling R-04) plus
003 personal-token CRUD (research R2, data-model C-1).

Hash scheme: ``pbkdf2_hmac(sha256, secret, salt, 100_000)`` with a random
16-byte salt. Stored in ``accounts.password_hash`` /
``personal_tokens.token_hash`` as::

    pbkdf2$<salt_hex>$<hash_hex>

``authenticate(db, owner, password)`` looks up the account by
``accounts.email = owner``, re-derives the digest, and compares with
``hmac.compare_digest``. Missing owner, NULL or malformed hash -> False
(no exception). ``hash_password`` is the helper that produces the stored
format (used by tests and by future password-setting, out of scope for 002).

003 multi-user personal tokens (R2):
- ``create_personal_token(db, account_email) -> (id, plaintext)``:
  generates ``os.urandom(32).hex()`` (64 hex chars, 256-bit entropy),
  stores only the pbkdf2 hash, returns the plaintext once.
- ``verify_personal_token(db, plaintext) -> account_email | None``:
  single pbkdf2 re-derivation + ``hmac.compare_digest``; sets
  ``last_used_at`` on success; None for revoked/unknown.
- ``list_personal_tokens(db, account_email=None)``: metadata only,
  never the hash.
- ``revoke_personal_token(db, token_id)``: flips ``revoked=1`` on that
  row only (US3 S3: revocation isolation).

The 001 test seed string ``pbkdf2:abc123`` (``tests/integration/test_upgrade.py``)
is a test fixture for the 001 upgrade path only — not a shipped format.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone

_HASH_PREFIX = "pbkdf2"
_ITERS = 100_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Derive and format a stored password hash.

    Returns ``pbkdf2$<salt_hex>$<hash_hex>`` with a fresh 16-byte salt.
    """
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _ITERS)
    return f"{_HASH_PREFIX}${salt.hex()}${digest.hex()}"


def authenticate(db, owner: str, password: str) -> bool:
    """Check ``password`` against the stored hash for ``owner``.

    ``db`` is a 001 state connection (accounts table). ``owner`` matches
    ``accounts.email``. Returns True only on a digest match; missing owner,
    NULL hash, or a malformed stored hash all return False.
    """
    row = db.execute(
        "SELECT password_hash FROM accounts WHERE email=?", (owner,)).fetchone()
    if row is None:
        return False
    stored = row[0]
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != _HASH_PREFIX:
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = parts[2]
    except ValueError:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _ITERS)
    return hmac.compare_digest(derived.hex(), expected)


# --- 003 multi-user: personal token & session helpers (T005 real) ---

def _now_iso() -> str:
    """UTC ISO-8601 timestamp, seconds precision (matches state.models._now)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_personal_token(db, account_email: str) -> tuple[int, str]:
    """Create a personal token for ``account_email`` (R2).

    Generates a 32-byte ``os.urandom(32).hex()`` token (64 hex chars,
    256-bit entropy), stores only its pbkdf2 hash in
    ``personal_tokens.token_hash``, and returns ``(id, plaintext)``.
    The plaintext is shown once at creation and never stored.
    """
    plaintext = os.urandom(32).hex()
    stored = hash_password(plaintext)
    cur = db.execute(
        "INSERT INTO personal_tokens (account_email, token_hash, created_at, revoked) "
        "VALUES (?, ?, ?, 0)",
        (account_email, stored, _now_iso()),
    )
    db.commit()
    return (cur.lastrowid, plaintext)


def verify_personal_token(db, plaintext: str) -> str | None:
    """Look up a personal token; return the account email or None (R2).

    Re-derives the pbkdf2 hash from the plaintext with the stored salt,
    constant-time compares with ``hmac.compare_digest``, sets
    ``last_used_at`` on success. Returns None for revoked, unknown, or
    malformed rows.
    """
    rows = db.execute(
        "SELECT account_email, token_hash, revoked FROM personal_tokens"
    ).fetchall()
    for row in rows:
        account_email, stored, revoked = row
        if revoked:
            continue
        if not stored:
            continue
        parts = stored.split("$")
        if len(parts) != 3 or parts[0] != _HASH_PREFIX:
            continue
        try:
            salt = bytes.fromhex(parts[1])
            expected = parts[2]
        except ValueError:
            continue
        derived = hashlib.pbkdf2_hmac(
            "sha256", plaintext.encode("utf-8"), salt, _ITERS)
        if hmac.compare_digest(derived.hex(), expected):
            db.execute(
                "UPDATE personal_tokens SET last_used_at=? WHERE token_hash=?",
                (_now_iso(), stored),
            )
            db.commit()
            return account_email
    return None


def list_personal_tokens(db, account_email: str | None = None) -> list[dict]:
    """List personal-token metadata (never the hash, R2 / data-model C-1).

    With ``account_email``: only that account's rows. Without: all rows.
    Each returned dict carries ``id``, ``account_email``, ``created_at``,
    ``last_used_at``, and ``revoked`` — the hash is deliberately omitted.
    """
    if account_email is not None:
        rows = db.execute(
            "SELECT id, account_email, created_at, last_used_at, revoked "
            "FROM personal_tokens WHERE account_email=? "
            "ORDER BY id",
            (account_email,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, account_email, created_at, last_used_at, revoked "
            "FROM personal_tokens ORDER BY id"
        ).fetchall()
    return [
        {
            "id": r[0],
            "account_email": r[1],
            "created_at": r[2],
            "last_used_at": r[3],
            "revoked": bool(r[4]),
        }
        for r in rows
    ]


def revoke_personal_token(db, token_id: int) -> None:
    """Flip ``revoked=1`` on the row with ``token_id`` (US3 S3: isolation).

    A no-op if the id does not exist (defensive: stale id from a
    previously-revoked or deleted row).
    """
    db.execute(
        "UPDATE personal_tokens SET revoked=1 WHERE id=?",
        (token_id,),
    )
    db.commit()


def verify_session(db, session_token: str) -> str | None:
    """Look up a session token; return the account email or None (R4). Stub — T006+."""
    raise NotImplementedError("verify_session: 003 T006")
