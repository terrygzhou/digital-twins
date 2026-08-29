"""Owner-scoping ACL predicate (feature 004, R5/R9, T008).

``can_access_schedule`` is the single swappable choke point between every
tool body and the owner/scope decision.  A future per-schedule ACL
replaces this body, not the call sites.
"""
from __future__ import annotations


def can_access_schedule(schedule_row: dict, caller_email: str,
                        caller_role: str) -> bool:
    """Owner-scoping check (R5/R9).

    Returns ``True`` iff the caller owns the schedule
    (``schedule_row["owner"] == caller_email``) **or** the caller's role
    is ``"admin"`` (R9: admin can see all schedules).

    Pure, stateless, no I/O.
    """
    if schedule_row["owner"] == caller_email:
        return True
    return caller_role == "admin"
