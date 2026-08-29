"""Password auth against the accounts store (002 ruling R-04).

Hash scheme: ``pbkdf2_hmac(sha256, password, salt, 100_000)`` with a random
16-byte salt. Stored in ``accounts.password_hash`` as::

    pbkdf2$<salt_hex>$<hash_hex>

``authenticate(db, owner, password)`` looks up the account by
``accounts.email = owner``, re-derives the digest, and compares with
``hmac.compare_digest``. Missing owner, NULL or malformed hash -> False
(no exception). ``hash_password`` is the helper that produces the stored
format (used by tests and by future password-setting, out of scope for 002).

The 001 test seed string ``pbkdf2:abc123`` (``tests/integration/test_upgrade.py``)
is a test fixture for the 001 upgrade path only — not a shipped format.
"""

from __future__ import annotations

import hashlib
import hmac
import os

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


# --- 003 multi-user: personal token & session helpers (T001 scaffold) ---

def verify_personal_token(db, token: str) -> str | None:
    """Look up a personal token; return the account email or None (R2). Stub — T004+."""
    raise NotImplementedError("verify_personal_token: 003 T004")


def verify_session(db, session_token: str) -> str | None:
    """Look up a session token; return the account email or None (R4). Stub — T005+."""
    raise NotImplementedError("verify_session: 003 T005")
