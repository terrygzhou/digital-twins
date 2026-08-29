"""Red tests for 003 multi-user: accounts CRUD (T003).

US1 S1/S2/S3 + SC-001 matrix rows + SC-002 last-admin guard:
first account -> admin, subsequent -> reader, duplicate email fails
with a named error and no second row, and the last admin cannot be
demoted or deleted.
"""

from datetime import datetime, timezone

import pytest

from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) in-memory-adjacent state DB with an accounts table."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


def _row(db, email):
    return db.execute(
        "SELECT role, password_hash, created_at, last_active "
        "FROM accounts WHERE email=?", (email,)
    ).fetchone()


def _count(db):
    return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def _utc_ok(ts: str) -> bool:
    """True if ts is a parseable ISO-8601 UTC timestamp (not the '' default)."""
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0


# --- next_role_for / create_account: first -> admin, else reader (R7) -------

def test_first_account_is_admin(db):
    from digital_twins.accounts import create_account, next_role_for
    assert next_role_for(db) == "admin"
    create_account(db, "first@example.com", "pw1")
    assert _row(db, "first@example.com")[0] == "admin"
    db.commit()
    assert next_role_for(db) == "reader"


def test_second_account_is_reader(db):
    from digital_twins.accounts import create_account
    create_account(db, "first@example.com", "pw1")
    create_account(db, "second@example.com", "pw2")
    db.commit()
    assert _row(db, "second@example.com")[0] == "reader"
    assert _row(db, "first@example.com")[0] == "admin"


def test_explicit_role_wins(db):
    from digital_twins.accounts import create_account
    create_account(db, "a@example.com", "pw", role="scheduler")
    db.commit()
    assert _row(db, "a@example.com")[0] == "scheduler"


# --- duplicate email: named error, no second row ----------------------------

def test_duplicate_email_fails_with_named_error(db):
    from digital_twins.accounts import DuplicateEmailError, create_account
    create_account(db, "dup@example.com", "pw1")
    db.commit()
    with pytest.raises(DuplicateEmailError):
        create_account(db, "dup@example.com", "pw2")
    assert _count(db) == 1


def test_duplicate_email_leaves_no_second_row(db):
    from digital_twins.accounts import DuplicateEmailError, create_account
    create_account(db, "dup@example.com", "pw1")
    db.commit()
    try:
        create_account(db, "dup@example.com", "other")
    except DuplicateEmailError:
        pass
    rows = db.execute(
        "SELECT email FROM accounts WHERE email=?", ("dup@example.com",)
    ).fetchall()
    assert len(rows) == 1


# --- password + timestamps ---------------------------------------------------

def test_password_hashed_with_auth_scheme(db):
    from digital_twins.accounts import create_account
    create_account(db, "pw@example.com", "hunter2")
    db.commit()
    stored = _row(db, "pw@example.com")[1]
    assert stored is not None and stored.startswith("pbkdf2$")
    from digital_twins.auth import authenticate
    assert authenticate(db, "pw@example.com", "hunter2")
    assert not authenticate(db, "pw@example.com", "wrong")


def test_created_at_and_last_active_set_to_now(db):
    from digital_twins.accounts import create_account
    before = datetime.now(timezone.utc).isoformat(timespec="seconds")
    create_account(db, "ts@example.com", "pw")
    db.commit()
    row = _row(db, "ts@example.com")
    created_at, last_active = row[2], row[3]
    assert created_at >= before, f"created_at {created_at!r} < {before!r}"
    assert _utc_ok(created_at)
    assert _utc_ok(last_active)


# --- get_role / set_role ------------------------------------------------------

def test_get_role(db):
    from digital_twins.accounts import create_account, get_role
    create_account(db, "first@example.com", "pw1")
    create_account(db, "second@example.com", "pw2")
    db.commit()
    assert get_role(db, "first@example.com") == "admin"
    assert get_role(db, "second@example.com") == "reader"
    assert get_role(db, "ghost@example.com") is None


def test_set_role(db):
    from digital_twins.accounts import create_account, set_role
    create_account(db, "first@example.com", "pw1")
    create_account(db, "second@example.com", "pw2")
    db.commit()
    set_role(db, "second@example.com", "scheduler")
    db.commit()
    assert _row(db, "second@example.com")[0] == "scheduler"
    assert _row(db, "first@example.com")[0] == "admin"


# --- delete_account / count_admins -------------------------------------------

def test_delete_account(db):
    from digital_twins.accounts import create_account, delete_account
    create_account(db, "first@example.com", "pw1")
    create_account(db, "gone@example.com", "pw2")
    db.commit()
    delete_account(db, "gone@example.com")
    db.commit()
    assert _count(db) == 1
    assert db.execute(
        "SELECT email FROM accounts WHERE email=?", ("gone@example.com",)
    ).fetchone() is None


def test_count_admins(db):
    from digital_twins.accounts import count_admins, create_account
    assert count_admins(db) == 0
    create_account(db, "first@example.com", "pw1")
    db.commit()
    assert count_admins(db) == 1
    create_account(db, "second@example.com", "pw2")
    db.commit()
    assert count_admins(db) == 1


# --- last_admin_guard (SC-002) ------------------------------------------------

def test_last_admin_guard_blocks_demoting_last_admin(db):
    from digital_twins.accounts import LastAdminError, create_account, set_role
    create_account(db, "first@example.com", "pw1")
    db.commit()
    with pytest.raises(LastAdminError):
        set_role(db, "first@example.com", "reader")
    db.commit()
    assert _row(db, "first@example.com")[0] == "admin"


def test_last_admin_guard_blocks_deleting_last_admin(db):
    from digital_twins.accounts import LastAdminError, create_account, delete_account
    create_account(db, "first@example.com", "pw1")
    db.commit()
    with pytest.raises(LastAdminError):
        delete_account(db, "first@example.com")
    db.commit()
    assert _count(db) == 1


def test_guard_allows_demote_delete_when_second_admin_exists(db):
    from digital_twins.accounts import create_account, delete_account, set_role
    create_account(db, "first@example.com", "pw1")   # admin
    create_account(db, "second@example.com", "pw2")  # reader
    db.commit()
    set_role(db, "second@example.com", "admin")
    db.commit()
    # Now two admins: demoting + deleting one must not trip the guard.
    set_role(db, "second@example.com", "reader")
    db.commit()
    assert _row(db, "second@example.com")[0] == "reader"
    delete_account(db, "second@example.com")
    db.commit()
    assert _count(db) == 1


def test_last_admin_guard_is_noop_for_non_admin(db):
    from digital_twins.accounts import create_account, delete_account, set_role
    create_account(db, "first@example.com", "pw1")
    create_account(db, "reader@example.com", "pw2")
    db.commit()
    # A reader is never "the last admin": no guard exception either way.
    set_role(db, "reader@example.com", "scheduler")
    delete_account(db, "reader@example.com")
    db.commit()
    assert _count(db) == 1
    assert _row(db, "first@example.com")[0] == "admin"
