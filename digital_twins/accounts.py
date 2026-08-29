"""Account CRUD + role model (003 multi-user).

T001 scaffold: ``ROLE_CAPS`` (R3 capability matrix) + ``owner_tag_for``
stub. T003 adds the CRUD layer on top of 001's ``accounts`` table
(v3-migrated, so ``created_at``/``last_active`` exist with ``''``
defaults): first row in ``accounts`` -> ``admin``, else ``reader``
(R7); password hashing reuses 001's ``auth.hash_password`` (R1);
duplicate email fails fast with a named error and no second row;
the last admin can neither be demoted nor deleted (SC-002).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .auth import hash_password

# Role capability matrix (R3). Populated by T003+.
ROLE_CAPS: dict[str, set[str]] = {}


def owner_tag_for(email: str) -> str:
    """Return the owner tag for an account (R6). Stub — T007+."""
    raise NotImplementedError("owner_tag_for: 003 T007")


class DuplicateEmailError(Exception):
    """Raised when an account is created with an email that already exists."""


class LastAdminError(Exception):
    """Raised when a demotion/deletion would leave the system with zero admins."""


def _now() -> str:
    """UTC ISO-8601 timestamp with seconds precision (mirrors state.models)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_role_for(db) -> str:
    """Resolve the role for a new account (R7): the first row in
    ``accounts`` becomes ``admin``; every later account is a ``reader``.
    """
    n = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    return "admin" if n == 0 else "reader"


def create_account(db, email: str, password: str, role: str | None = None):
    """Create an account row (001's ``accounts`` table).

    ``role`` defaults to ``next_role_for(db)`` (first row -> admin, else
    reader). The password is hashed via 001's ``auth.hash_password`` (R1)
    and ``created_at``/``last_active`` are set to now (UTC ISO-8601).
    A duplicate email raises :class:`DuplicateEmailError` and leaves no
    second row.
    """
    if role is None:
        role = next_role_for(db)
    now = _now()
    try:
        db.execute(
            "INSERT INTO accounts (email, role, password_hash, "
            "created_at, last_active) VALUES (?, ?, ?, ?, ?)",
            (email, role, hash_password(password), now, now),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise DuplicateEmailError(
            f"account already exists: {email}"
        ) from exc


def get_role(db, email: str) -> str | None:
    """The stored role for ``email``, or ``None`` if no such account."""
    row = db.execute(
        "SELECT role FROM accounts WHERE email=?", (email,)).fetchone()
    return row[0] if row else None


def set_role(db, email: str, role: str) -> None:
    """Change an account's role. Refused (``LastAdminError``) when the
    account is the last admin and ``role != 'admin'`` (SC-002).
    """
    last_admin_guard(db, email, new_role=role)
    db.execute(
        "UPDATE accounts SET role=? WHERE email=?", (role, email))
    db.commit()


def delete_account(db, email: str) -> None:
    """Delete an account. Refused (``LastAdminError``) when the account
    is the last admin (SC-002).
    """
    last_admin_guard(db, email, delete=True)
    db.execute("DELETE FROM accounts WHERE email=?", (email,))
    db.commit()


def count_admins(db) -> int:
    """Number of accounts currently holding the ``admin`` role."""
    return db.execute(
        "SELECT COUNT(*) FROM accounts WHERE role='admin'").fetchone()[0]


def last_admin_guard(db, email: str, new_role: str | None = None,
                     delete: bool = False) -> None:
    """Raise :class:`LastAdminError` when ``email`` is the only admin and
    the pending action would leave the system with zero admins.

    No-op for non-admins, or when a second admin survives the action.
    """
    row = db.execute(
        "SELECT role FROM accounts WHERE email=?", (email,)).fetchone()
    if row is None or row[0] != "admin":
        return
    if count_admins(db) > 1:
        return
    if delete:
        raise LastAdminError(
            f"cannot delete the last admin: {email}")
    if new_role != "admin":
        raise LastAdminError(
            f"cannot demote the last admin: {email}")
