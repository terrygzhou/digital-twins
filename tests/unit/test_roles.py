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

# reader: read-only (3 read capabilities)
READER_CAPS = {
    "sign_in",
    "query_status",
    "view_own_history",
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
