"""T007 (RED): can_access_schedule — owner-scoping ACL predicate (feature 004).

Asserts ``can_access_schedule(schedule_row, caller_email, caller_role) -> bool``:

- True when the caller is the schedule owner (``schedule_row["owner"] ==
  caller_email``).
- True when the caller's role is ``"admin"`` (R9: admin can see all
  schedules).
- False otherwise (a non-owner, non-admin caller cannot access another
  user's schedule).

Pure function, no I/O — the test uses plain dicts and strings, no DB.
"""
from __future__ import annotations

import pytest


def _row(owner: str) -> dict:
    """A minimal schedule row dict (the ACL predicate only reads 'owner')."""
    return {"id": 1, "owner": owner, "source": "hermes", "preset": "daily"}


# ---------------------------------------------------------------------------
# T007: owner → True
# ---------------------------------------------------------------------------

def test_owner_returns_true():
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("alice@example.com")
    assert can_access_schedule(row, "alice@example.com", "reader") is True


def test_owner_returns_true_even_if_reader_role():
    """Ownership is sufficient; the role does not matter for the owner."""
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("bob@example.com")
    assert can_access_schedule(row, "bob@example.com", "reader") is True


# ---------------------------------------------------------------------------
# T007: admin (non-owner) → True
# ---------------------------------------------------------------------------

def test_admin_non_owner_returns_true():
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("alice@example.com")
    assert can_access_schedule(row, "charlie@example.com", "admin") is True


def test_admin_non_owner_returns_true_even_scheduler_role_mismatch():
    """Admin role alone is sufficient — the admin need not own the schedule."""
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("system")
    assert can_access_schedule(row, "root@example.com", "admin") is True


# ---------------------------------------------------------------------------
# T007: non-owner, non-admin → False
# ---------------------------------------------------------------------------

def test_non_owner_reader_returns_false():
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("alice@example.com")
    assert can_access_schedule(row, "bob@example.com", "reader") is False


def test_non_owner_scheduler_returns_false():
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("alice@example.com")
    assert can_access_schedule(row, "bob@example.com", "scheduler") is False


def test_non_owner_unknown_role_returns_false():
    """An unknown role (not in ROLE_CAPS) is denied (degrades to
    reader-equivalent per accounts.guard)."""
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("alice@example.com")
    assert can_access_schedule(row, "bob@example.com", "ghost") is False


# ---------------------------------------------------------------------------
# T007: admin AND owner → True (both conditions, still True)
# ---------------------------------------------------------------------------

def test_admin_owner_returns_true():
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("admin@example.com")
    assert can_access_schedule(row, "admin@example.com", "admin") is True


# ---------------------------------------------------------------------------
# T007: pure — no side effects, no I/O
# ---------------------------------------------------------------------------

def test_pure_no_mutation():
    """The predicate must not mutate the schedule_row dict."""
    from digital_twins.mcp.acl import can_access_schedule
    row = _row("alice@example.com")
    snapshot = dict(row)
    can_access_schedule(row, "alice@example.com", "reader")
    can_access_schedule(row, "bob@example.com", "admin")
    can_access_schedule(row, "bob@example.com", "reader")
    assert row == snapshot
