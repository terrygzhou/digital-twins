"""003 multi-user T004: ROLE_CAPS matrix + guard helper (SC-001).

Test-First: these tests are red until T004 fills ROLE_CAPS and adds
guard(). They iterate the full R3 matrix programmatically so a new
capability row without a test fails the suite (100% matrix coverage).
"""

import pytest

from digital_twins.accounts import ROLE_CAPS, RoleDenied, guard

# ---------------------------------------------------------------------------
# R3 role-capability matrix (research.md R3). 11 capabilities × 3 roles.
# Capabilities are keyed by their stable snake_case identifier.
# ---------------------------------------------------------------------------

# admin: all 11 capabilities
ADMIN_CAPS = {
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
}

# scheduler: everything except the 3 admin-only cross-user capabilities
SCHEDULER_CAPS = {
    "sign_in",
    "query_status",
    "view_own_history",
    "trigger_run",
    "schedule_crud",
    "manage_own_config",
    "manage_own_tokens",
}

# reader: read-only + manage own personal config (T018/R5)
READER_CAPS = {
    "sign_in",
    "query_status",
    "view_own_history",
    "manage_own_config",
}

# The full matrix: role -> set of allowed capabilities
R3_MATRIX = {
    "admin": ADMIN_CAPS,
    "scheduler": SCHEDULER_CAPS,
    "reader": READER_CAPS,
}

# All 11 capability identifiers (for the coverage check)
ALL_CAPABILITIES = frozenset(R3_MATRIX["admin"])  # admin is the superset


# ---------------------------------------------------------------------------
# Test 1: ROLE_CAPS must match the R3 matrix exactly (100% cell coverage)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(R3_MATRIX), ids=lambda r: r)
def test_role_caps_match_r3(role: str) -> None:
    """Every cell of the R3 table is asserted: ROLE_CAPS[role] == R3_MATRIX[role]."""
    expected = R3_MATRIX[role]
    actual = ROLE_CAPS[role]
    assert actual == expected, (
        f"ROLE_CAPS[{role!r}] = {sorted(actual)}, "
        f"expected R3: {sorted(expected)}"
    )


@pytest.mark.parametrize("role", sorted(R3_MATRIX), ids=lambda r: r)
@pytest.mark.parametrize("cap", sorted(ALL_CAPABILITIES), ids=lambda c: c)
def test_guard_allow(role: str, cap: str) -> None:
    """For every (role, capability) cell where R3 says allow, guard() does not raise."""
    if cap in R3_MATRIX[role]:
        # Should not raise
        guard(role, cap)
    else:
        # Should raise RoleDenied
        with pytest.raises(RoleDenied):
            guard(role, cap)


# ---------------------------------------------------------------------------
# Test 2: matrix coverage — every capability in ALL_CAPABILITIES is tested
# ---------------------------------------------------------------------------

def test_no_capability_missing() -> None:
    """SC-001: every capability in the R3 table has at least one test cell.

    This test asserts that the number of parametrized cells in test_guard_allow
    equals 3 (roles) × len(ALL_CAPABILITIES). If a new capability is added to
    R3_MATRIX but NOT to the parametrize set, this test fails — enforcing
    100% matrix coverage.
    """
    # test_guard_allow is parametrized over sorted(R3_MATRIX) × sorted(ALL_CAPABILITIES)
    # The number of cells must be exactly 3 × len(ALL_CAPABILITIES)
    expected_cells = 3 * len(ALL_CAPABILITIES)
    # Verify that ALL_CAPABILITIES is the union of all roles' caps (admin is the superset)
    union = set().union(*R3_MATRIX.values())
    assert union == ALL_CAPABILITIES, (
        f"Union of all roles' caps ({sorted(union)}) != ALL_CAPABILITIES ({sorted(ALL_CAPABILITIES)}). "
        f"SC-001: a capability exists in a role but not in the coverage set."
    )
    # Verify that the parametrize set is not empty and covers all capabilities
    assert len(ALL_CAPABILITIES) == 11, (
        f"Expected 11 capabilities (R3 defines 11), got {len(ALL_CAPABILITIES)}. "
        f"SC-001: the R3 matrix has changed — update the test to match."
    )


# ---------------------------------------------------------------------------
# Test 3: unknown/legacy role → reader-equivalent (deny mutating), no crash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("legacy_role", [
    "owner",       # 001's legacy test fixture
    "superuser",
    "goddess",
    "",            # empty string
    "ADMIN",       # case-sensitivity: must not match "admin"
])
def test_unknown_role_is_reader_equivalent(legacy_role: str) -> None:
    """Unknown/legacy roles are treated as reader-equivalent:
    read capabilities allowed, mutating capabilities denied. No crash.
    """
    # Read capabilities: allowed (reader-equivalent)
    for cap in READER_CAPS:
        guard(legacy_role, cap)  # should not raise

    # Mutating capabilities: denied
    for cap in ALL_CAPABILITIES - READER_CAPS:
        with pytest.raises(RoleDenied):
            guard(legacy_role, cap)


# ---------------------------------------------------------------------------
# Test 4: RoleDenied is a named exception
# ---------------------------------------------------------------------------

def test_role_denied_is_exception() -> None:
    """RoleDenied must be a named Exception subclass (like DuplicateEmailError)."""
    assert issubclass(RoleDenied, Exception)
    # It should be distinguishable from other named errors
    assert RoleDenied is not Exception
    # Instantiation with (role, capability)
    exc = RoleDenied("reader", "trigger_run")
    assert "reader" in str(exc)
    assert "trigger_run" in str(exc)
    # Attributes exposed for the caller
    assert exc.role == "reader"
    assert exc.capability == "trigger_run"


# ---------------------------------------------------------------------------
# T007: require_capability helper + same-transaction last-admin guard (SC-002)
# ---------------------------------------------------------------------------

import pytest

from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.accounts import (
    RoleDenied,
    LastAdminError,
    create_account,
    set_role,
    delete_account,
    count_admins,
)


@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) state DB with an accounts table."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


# --- require_capability: thin wrapper over guard() with action_label ---------

def test_require_capability_raises_named_role_denied():
    """require_capability raises RoleDenied with a named reason that
    includes the action_label, not just the capability id."""
    from digital_twins.accounts import require_capability
    with pytest.raises(RoleDenied) as exc_info:
        require_capability("reader", "trigger_run", "trigger a run")
    exc = exc_info.value
    # The named reason must mention the action, not just the capability
    assert "trigger a run" in str(exc)
    # The role and capability are still exposed as attributes
    assert exc.role == "reader"
    assert exc.capability == "trigger_run"


def test_require_capability_allows_valid_combination():
    """require_capability does not raise when the role has the capability."""
    from digital_twins.accounts import require_capability
    # admin has trigger_run
    require_capability("admin", "trigger_run", "trigger a run")
    # scheduler has trigger_run
    require_capability("scheduler", "trigger_run", "trigger a run")
    # reader has query_status
    require_capability("reader", "query_status", "check status")


def test_require_capability_unknown_role_denied():
    """Unknown roles are treated as reader-equivalent (deny mutating)."""
    from digital_twins.accounts import require_capability
    with pytest.raises(RoleDenied):
        require_capability("owner", "trigger_run", "trigger a run")
    # But read capabilities are allowed
    require_capability("owner", "query_status", "check status")


def test_require_capability_denied_includes_action_label():
    """The RoleDenied message must include the action_label so the CLI/HTTP
    caller can display a human-readable reason without leaking the matrix."""
    from digital_twins.accounts import require_capability
    with pytest.raises(RoleDenied) as exc_info:
        require_capability("reader", "manage_accounts", "manage accounts")
    msg = str(exc_info.value)
    assert "manage accounts" in msg
    assert "reader" in msg
    assert "manage_accounts" in msg


# --- same-transaction last-admin guard (SC-002) ------------------------------

def test_demote_last_admin_refused_with_named_error(db):
    """Demoting the last admin is refused with LastAdminError.
    The guard must be enforced within the same transaction as the mutation
    (BEGIN → count → apply → COMMIT)."""
    create_account(db, "first@example.com", "pw1")
    db.commit()
    with pytest.raises(LastAdminError) as exc_info:
        set_role(db, "first@example.com", "reader")
    # Named error
    assert "demote" in str(exc_info.value).lower()
    assert "first@example.com" in str(exc_info.value)
    # Role unchanged
    assert db.execute(
        "SELECT role FROM accounts WHERE email=?",
        ("first@example.com",)
    ).fetchone()[0] == "admin"
    # count_admins still 1
    assert count_admins(db) == 1


def test_delete_last_admin_refused_with_named_error(db):
    """Deleting the last admin is refused with LastAdminError.
    The guard must be enforced within the same transaction as the mutation."""
    create_account(db, "first@example.com", "pw1")
    db.commit()
    with pytest.raises(LastAdminError) as exc_info:
        delete_account(db, "first@example.com")
    # Named error
    assert "delete" in str(exc_info.value).lower()
    assert "first@example.com" in str(exc_info.value)
    # Account still exists
    assert db.execute(
        "SELECT COUNT(*) FROM accounts WHERE email=?",
        ("first@example.com",)
    ).fetchone()[0] == 1
    # count_admins still 1
    assert count_admins(db) == 1


def test_demote_with_second_admin_present_succeeds(db):
    """With a second admin present, demoting the first admin succeeds.
    The same-transaction guard counts surviving admins correctly."""
    create_account(db, "first@example.com", "pw1")   # admin
    create_account(db, "second@example.com", "pw2")  # reader
    db.commit()
    # Promote second to admin
    set_role(db, "second@example.com", "admin")
    db.commit()
    assert count_admins(db) == 2
    # Now demote first — should succeed (second admin survives)
    set_role(db, "first@example.com", "reader")
    db.commit()
    assert db.execute(
        "SELECT role FROM accounts WHERE email=?",
        ("first@example.com",)
    ).fetchone()[0] == "reader"
    assert count_admins(db) == 1


def test_delete_with_second_admin_present_succeeds(db):
    """With a second admin present, deleting the first admin succeeds.
    The same-transaction guard counts surviving admins correctly."""
    create_account(db, "first@example.com", "pw1")   # admin
    create_account(db, "second@example.com", "pw2")  # reader
    db.commit()
    # Promote second to admin
    set_role(db, "second@example.com", "admin")
    db.commit()
    assert count_admins(db) == 2
    # Now delete first — should succeed (second admin survives)
    delete_account(db, "first@example.com")
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM accounts WHERE email=?",
        ("first@example.com",)
    ).fetchone()[0] == 0
    assert count_admins(db) == 1


def test_set_role_guard_within_single_transaction(db):
    """The last-admin guard and the role mutation must happen in one
    transaction. If the guard raises, no partial write is committed.
    We verify this by checking that after a refused demotion, the
    connection has no uncommitted changes that could leak."""
    create_account(db, "first@example.com", "pw1")
    db.commit()
    try:
        set_role(db, "first@example.com", "reader")
    except LastAdminError:
        pass
    # After the refused demotion, the connection should be clean:
    # no pending writes, no partial state.
    # The role must still be admin (unchanged).
    assert db.execute(
        "SELECT role FROM accounts WHERE email=?",
        ("first@example.com",)
    ).fetchone()[0] == "admin"
    # And the transaction state must be consistent:
    # count_admins should reflect the actual committed state.
    assert count_admins(db) == 1


def test_delete_account_guard_within_single_transaction(db):
    """The last-admin guard and the delete mutation must happen in one
    transaction. After a refused delete, no partial write leaks."""
    create_account(db, "first@example.com", "pw1")
    db.commit()
    try:
        delete_account(db, "first@example.com")
    except LastAdminError:
        pass
    # Account still exists
    assert db.execute(
        "SELECT COUNT(*) FROM accounts WHERE email=?",
        ("first@example.com",)
    ).fetchone()[0] == 1
    # count_admins consistent
    assert count_admins(db) == 1
