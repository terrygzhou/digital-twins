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

# Role capability matrix (R3). T004 fills the full 11-capability × 3-role table.
# Capability identifiers are stable snake_case strings; see research.md R3.
ROLE_CAPS: dict[str, set[str]] = {
    "admin": {
        "sign_in",
        "query_status",
        "view_own_history",
        "view_all_history",
        "trigger_run",
        "schedule_crud",
        "manage_own_config",
        "manage_own_tokens",
        "write_global_config",
        "manage_accounts",
        "manage_all_user_config",
    },
    "scheduler": {
        "sign_in",
        "query_status",
        "view_own_history",
        "trigger_run",
        "schedule_crud",
        "manage_own_config",
        "manage_own_tokens",
    },
    "reader": {
        "sign_in",
        "query_status",
        "view_own_history",
    },
}


class RoleDenied(Exception):
    """Raised when a role lacks a required capability (R3 matrix).

    Mirrors :class:`DuplicateEmailError` / :class:`LastAdminError`:
    a named exception the caller can catch to produce a 403 / exit-2
    response without leaking the full matrix.

    When ``action_label`` is provided (via :func:`require_capability`),
    the message becomes human-readable:
    ``"role 'reader' may not trigger a run (capability 'trigger_run')"``.
    Without it, the message is the terse
    ``"role 'reader' lacks capability 'trigger_run'"``.
    """
    def __init__(self, role: str, capability: str,
                 action_label: str | None = None):
        self.role = role
        self.capability = capability
        self.action_label = action_label
        if action_label:
            super().__init__(
                f"role {role!r} may not {action_label} "
                f"(capability {capability!r})"
            )
        else:
            super().__init__(
                f"role {role!r} lacks capability {capability!r}"
            )


def guard(role: str, capability: str) -> None:
    """Raise :class:`RoleDenied` if ``role`` lacks ``capability`` (R3).

    Unknown/legacy roles (e.g. ``"owner"`` from 001's test fixtures) are
    treated as reader-equivalent: read capabilities allowed, mutating
    capabilities denied. No crash, no KeyError — the guard degrades
    gracefully to the most restrictive known profile.
    """
    caps = ROLE_CAPS.get(role, ROLE_CAPS["reader"])
    if capability not in caps:
        raise RoleDenied(role, capability)


def require_capability(role: str, capability: str,
                       action_label: str) -> None:
    """Raise :class:`RoleDenied` with a named reason if ``role`` lacks
    ``capability`` (T007 — the thin wrapper the CLI/HTTP boundary calls).

    Wraps :func:`guard` so the caller gets a human-readable reason that
    includes the *action label* (e.g. ``"trigger a run"``) rather than
    just the capability id.  The action label is what the CLI/HTTP
    response shows; the capability id is what the matrix checks.

    >>> require_capability("reader", "trigger_run", "trigger a run")
    RoleDenied: role 'reader' may not trigger a run (capability 'trigger_run')

    >>> require_capability("admin", "trigger_run", "trigger a run")
    # no exception
    """
    try:
        guard(role, capability)
    except RoleDenied:
        raise RoleDenied(
            role,
            capability,
            action_label=action_label,
        ) from None


def owner_tag_for(email: str) -> str:
    """Return the owner tag for an account (R6).

    The tag is ``f"{email}-ingest"`` — the stable, query-time filter value
    stamped on every Qdrant point the account ingests (T016).  The pipeline
    builds the tag inline (``f"{owner}-ingest"``); this helper is the
    single-source for callers outside the pipeline (e.g. the "mine" filter
    in T019's ``list_runs``) so the tag format is defined in one place.
    """
    return f"{email}-ingest"


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

    The guard and the mutation happen in the *same transaction*
    (BEGIN → count → apply → COMMIT): if the guard raises, the
    connection is rolled back and no partial write leaks.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        last_admin_guard(db, email, new_role=role)
        db.execute(
            "UPDATE accounts SET role=? WHERE email=?", (role, email))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def delete_account(db, email: str) -> None:
    """Delete an account. Refused (``LastAdminError``) when the account
    is the last admin (SC-002).

    The guard and the mutation happen in the *same transaction*
    (BEGIN → count → apply → COMMIT): if the guard raises, the
    connection is rolled back and no partial write leaks.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        last_admin_guard(db, email, delete=True)
        db.execute("DELETE FROM accounts WHERE email=?", (email,))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


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
