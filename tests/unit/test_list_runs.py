"""Red tests for 003 multi-user: run-history "mine" filter (T019, US3).

Covers ``digital_twins.state.models.list_runs``:
- ``list_runs(db, user="alice@example.com")`` returns only alice's rows
  (the "mine" view: ``audit_runs WHERE scheduled_by = <user>``).
- ``list_runs(db, user="bob@example.com")`` returns only bob's rows.
- ``list_runs(db, all_users=True)`` returns every row (admin-only;
  checked via the ``view_all_history`` capability in ``ROLE_CAPS``).
- A ``system``-attributed run is in the admin's ``all_users`` view but
  in no one's "mine" view.
- ``all_users=True`` with a non-admin caller raises ``RoleDenied``.
"""

import pytest

from digital_twins.accounts import RoleDenied
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.state.models import (
    apply_v1,
    apply_v2,
    finish_audit_run,
    start_audit_run,
)


@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) state DB with accounts + audit_runs tables."""
    conn = connect(tmp_path)
    apply_v1(conn)
    apply_v2(conn)
    migrate(conn)  # applies v3
    yield conn
    conn.close()


def _insert_account(db, email: str, role: str) -> None:
    """Insert an accounts row directly (bypasses create_account)."""
    db.execute(
        "INSERT INTO accounts (email, role, password_hash, "
        "created_at, last_active) VALUES (?, ?, ?, '', '')",
        (email, role, "dummy-hash"),
    )
    db.commit()


def _insert_run(db, run_id: str, scheduled_by: str,
                trigger: str = "manual") -> None:
    """Insert a completed audit_runs row."""
    start_audit_run(db, run_id, trigger=trigger, scheduled_by=scheduled_by)
    finish_audit_run(db, run_id, "ok", {"hermes": 5})


# --- mine view ---------------------------------------------------------------

def test_list_runs_mine_alice(db):
    """list_runs(user=alice) returns only alice's rows."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_account(db, "bob@example.com", "reader")

    # Interleaved runs
    _insert_run(db, "run-001", "alice@example.com")
    _insert_run(db, "run-002", "bob@example.com")
    _insert_run(db, "run-003", "alice@example.com")
    _insert_run(db, "run-004", "bob@example.com")

    from digital_twins.state.models import list_runs
    rows = list_runs(db, user="alice@example.com")
    run_ids = {r[0] for r in rows}
    assert run_ids == {"run-001", "run-003"}, (
        f"expected only alice's runs, got {run_ids}"
    )


def test_list_runs_mine_bob(db):
    """list_runs(user=bob) returns only bob's rows."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_account(db, "bob@example.com", "reader")

    _insert_run(db, "run-001", "alice@example.com")
    _insert_run(db, "run-002", "bob@example.com")
    _insert_run(db, "run-003", "alice@example.com")
    _insert_run(db, "run-004", "bob@example.com")

    from digital_twins.state.models import list_runs
    rows = list_runs(db, user="bob@example.com")
    run_ids = {r[0] for r in rows}
    assert run_ids == {"run-002", "run-004"}, (
        f"expected only bob's runs, got {run_ids}"
    )


# --- all_users (admin-only) --------------------------------------------------

def test_list_runs_all_users_admin(db):
    """list_runs(all_users=True) with admin returns every row."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_account(db, "bob@example.com", "reader")

    _insert_run(db, "run-001", "alice@example.com")
    _insert_run(db, "run-002", "bob@example.com")
    _insert_run(db, "run-003", "alice@example.com")
    _insert_run(db, "run-004", "bob@example.com")

    from digital_twins.state.models import list_runs
    rows = list_runs(db, user="alice@example.com", all_users=True)
    run_ids = {r[0] for r in rows}
    assert run_ids == {"run-001", "run-002", "run-003", "run-004"}, (
        f"expected all 4 runs, got {run_ids}"
    )


def test_list_runs_all_users_non_admin_denied(db):
    """list_runs(all_users=True) with a non-admin raises RoleDenied."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_account(db, "bob@example.com", "reader")

    _insert_run(db, "run-001", "alice@example.com")
    _insert_run(db, "run-002", "bob@example.com")

    from digital_twins.state.models import list_runs
    with pytest.raises(RoleDenied):
        list_runs(db, user="bob@example.com", all_users=True)


# --- system runs -------------------------------------------------------------

def test_system_run_in_all_users_view(db):
    """A system run appears in the admin's all_users view."""
    _insert_account(db, "alice@example.com", "admin")

    _insert_run(db, "run-001", "alice@example.com")
    _insert_run(db, "run-002", "system")

    from digital_twins.state.models import list_runs
    rows = list_runs(db, user="alice@example.com", all_users=True)
    run_ids = {r[0] for r in rows}
    assert "run-002" in run_ids, (
        f"system run should be in all_users view, got {run_ids}"
    )
    assert "run-001" in run_ids


def test_system_run_not_in_any_mine_view(db):
    """A system run is in no one's 'mine' view."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_account(db, "bob@example.com", "reader")

    _insert_run(db, "run-001", "alice@example.com")
    _insert_run(db, "run-002", "bob@example.com")
    _insert_run(db, "run-003", "system")

    from digital_twins.state.models import list_runs

    alice_rows = list_runs(db, user="alice@example.com")
    assert "run-003" not in {r[0] for r in alice_rows}, (
        "system run must not be in alice's mine view"
    )

    bob_rows = list_runs(db, user="bob@example.com")
    assert "run-003" not in {r[0] for r in bob_rows}, (
        "system run must not be in bob's mine view"
    )


# --- no args (no user, no all_users) ------------------------------------------

def test_list_runs_no_user_no_all_users_returns_empty(db):
    """list_runs() with no user and no all_users returns empty list."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_run(db, "run-001", "alice@example.com")

    from digital_twins.state.models import list_runs
    rows = list_runs(db)
    assert rows == [], (
        f"expected empty list, got {len(rows)} rows"
    )


# --- edge: user with no runs ---------------------------------------------------

def test_list_runs_mine_no_runs(db):
    """list_runs(user=...) for a user with no runs returns empty list."""
    _insert_account(db, "alice@example.com", "admin")
    _insert_account(db, "bob@example.com", "reader")
    _insert_run(db, "run-001", "alice@example.com")

    from digital_twins.state.models import list_runs
    rows = list_runs(db, user="bob@example.com")
    assert rows == []
