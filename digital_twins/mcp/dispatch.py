"""MCP tool executor (feature 004, R4/R5/R9, T010).

``dispatch(ctx, tool_name, args)`` runs the per-tool pipeline:

1. **Role-gate** via ``accounts.require_capability``.
2. **Owner-scope** via ``can_access_schedule`` (when the tool targets a
   schedule).
3. **Tool body** — a callable from the ``tool_bodies`` dict.
4. **Audit row** — written where applicable (Phase 3/4 wires the real
   audit writes; for now the stub bodies handle their own audit).
5. **Return** the result dict.

For Phase 2 the tool bodies are stubs: each returns
``{"ok": True, "tool": <name>}``.  The real bodies land in Phase 3/4.
The role-gate and owner-scope logic is what T009/T010 test.
"""
from __future__ import annotations

from typing import Any, Callable

from ..accounts import require_capability, RoleDenied
from .acl import can_access_schedule
from .registry import MCPContext


# ---------------------------------------------------------------------------
# tool → capability mapping (R4 role-gate)
# ---------------------------------------------------------------------------

TOOL_CAPABILITIES: dict[str, str] = {
    "kb_schedule_list": "query_status",
    "kb_schedule_create": "schedule_crud",
    "kb_schedule_update": "schedule_crud",
    "kb_schedule_delete": "schedule_crud",
    "kb_schedule_run": "trigger_run",
    "kb_run_history": "view_own_history",
}

# Tools that target a specific schedule and therefore need owner-scope
# (R5).  ``kb_schedule_list`` is special: it either lists the caller's own
# schedules or (admin, all_users=True) lists all — the owner-scope check
# is applied per-row inside the body, not as a pre-gate here.
# ``kb_run_history`` targets runs, not schedules.
_SCHEDULE_TARGETING: set[str] = {
    "kb_schedule_update",
    "kb_schedule_delete",
    "kb_schedule_run",
}

# ---------------------------------------------------------------------------
# tool bodies (Phase 2: stubs + the real kb_schedule_list)
# ---------------------------------------------------------------------------

def _stub_body(name: str) -> dict:
    """A stub tool body: returns ``{"ok": True, "tool": <name>}``."""
    return {"ok": True, "tool": name}


def _to_r2(schedule: dict) -> dict:
    """Map a raw schedules-row dict to the R2 MCP-facing shape.

    The raw 002/003 helper returns the DB column name ``id``; the R2
    contract (004 contracts/scheduler.md, mirroring 003) exposes it as
    ``schedule_id``.  All other field names are already R2-compatible.
    """
    result = dict(schedule)
    result["schedule_id"] = result.pop("id")
    return result


def _kb_schedule_list_body(ctx: MCPContext, args: dict) -> dict:
    """List schedules: own scope by default; all_users=True is admin-only.

    When ``all_users=True``, the caller must have ``view_all_history``
    (admin-only, R9).  The role-gate in dispatch already checked
    ``query_status``; this body adds the elevated ``view_all_history``
    check for the all-users path.
    """
    from ..scheduler.schedules import list_schedules

    all_users = args.get("all_users", False)
    if all_users:
        # Elevated role-gate: admin-only (R9).
        try:
            require_capability(
                ctx.caller_role,
                "view_all_history",
                action_label="list all users' schedules",
            )
        except RoleDenied as exc:
            return {
                "ok": False,
                "error": {
                    "code": "permission_denied",
                    "message": str(exc),
                },
            }
        schedules = list_schedules(ctx.db)
    else:
        schedules = list_schedules(ctx.db, owner=ctx.caller_email)

    # Filter by can_access_schedule (a no-op for the owner, a pass-through
    # for admin; guards against a future ACL that narrows visibility).
    schedules = [
        s for s in schedules
        if can_access_schedule(s, ctx.caller_email, ctx.caller_role)
    ]

    # R2: expose the id as schedule_id (contract field name).
    return {"ok": True, "data": {"schedules": [_to_r2(s) for s in schedules]}}


def _kb_schedule_create_body(ctx: MCPContext, args: dict) -> dict:
    """Create a schedule for the caller (owner = caller_email, R5).

    Delegates to ``scheduler.schedules.create_schedule`` (002 helper).
    The caller's email is the owner (v1: caller-scoped, R5).  The ``acl``
    argument is passed through to ``create_schedule`` (default ``"owner"``).

    R11: no audit_runs row (a CRUD mutation is not a run).
    """
    from ..scheduler.schedules import create_schedule

    source = args.get("source")
    preset = args.get("preset")
    param = args.get("param")
    fire_time = args.get("fire_time", "03:00")
    acl = args.get("acl", "owner")

    try:
        schedule = create_schedule(
            ctx.db,
            owner=ctx.caller_email,
            source=source,
            preset=preset,
            param=param,
            fire_time=fire_time,
            acl=acl,
        )
    except ValueError as exc:
        # Map the 002 preset/param validation errors to the R4 error codes.
        if "unknown preset" in str(exc):
            return {
                "ok": False,
                "error": {
                    "code": "invalid_preset",
                    "message": str(exc),
                },
            }
        return {
            "ok": False,
            "error": {
                "code": "invalid_param",
                "message": str(exc),
            },
        }

    return {"ok": True, "data": {"schedule": _to_r2(schedule)}}


TOOL_BODIES: dict[str, Callable[..., dict]] = {
    "kb_schedule_list": _kb_schedule_list_body,
    "kb_schedule_create": _kb_schedule_create_body,
    "kb_schedule_update": lambda ctx, args: _stub_body("kb_schedule_update"),
    "kb_schedule_delete": lambda ctx, args: _stub_body("kb_schedule_delete"),
    "kb_schedule_run": lambda ctx, args: _stub_body("kb_schedule_run"),
    "kb_run_history": lambda ctx, args: _stub_body("kb_run_history"),
    # BR-10 stubs
    "kb_search": lambda ctx, args: {
        "ok": False,
        "error": {
            "code": "not_implemented_yet",
            "message": "kb_search is a BR-10 tool, a follow-up slice; "
                       "not implemented in 004",
        },
    },
    "kb_chat": lambda ctx, args: {
        "ok": False,
        "error": {
            "code": "not_implemented_yet",
            "message": "kb_chat is a BR-10 tool, a follow-up slice; "
                       "not implemented in 004",
        },
    },
    "kb_ingest": lambda ctx, args: {
        "ok": False,
        "error": {
            "code": "not_implemented_yet",
            "message": "kb_ingest is a BR-10 tool, a follow-up slice; "
                       "not implemented in 004",
        },
    },
    "kb_health": lambda ctx, args: {
        "ok": False,
        "error": {
            "code": "not_implemented_yet",
            "message": "kb_health is a BR-10 tool, a follow-up slice; "
                       "not implemented in 004",
        },
    },
}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def dispatch(ctx: MCPContext, tool_name: str, args: dict) -> dict:
    """Execute an MCP tool call.

    Parameters
    ----------
    ctx:
        The authenticated caller context (db, caller_email, caller_role,
        agent_kind).
    tool_name:
        The tool to call (one of the ten in the registry).
    args:
        The tool's argument dict (as passed by the MCP client).

    Returns
    -------
    dict
        The tool's result.  On success: ``{"ok": True, ...}``.
        On denial: ``{"ok": False, "error": {"code": ..., "message": ...}}``.

    Order of operations (R4/R5/R9):
        1. Role-gate: ``require_capability`` (``permission_denied`` on
           denial).
        2. Owner-scope: ``can_access_schedule`` for schedule-targeting
           tools (``schedule_not_found`` on denial).
        3. Run the tool body.
        4. Return the result.
    """
    # Unknown tool → internal_error (not in the capability map, not a
    # BR-10 stub).
    if tool_name not in TOOL_CAPABILITIES and tool_name not in TOOL_BODIES:
        return {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": f"unknown tool: {tool_name}",
            },
        }

    # --- 1. Role-gate (R4) ------------------------------------------------
    if tool_name in TOOL_CAPABILITIES:
        capability = TOOL_CAPABILITIES[tool_name]
        try:
            require_capability(
                ctx.caller_role,
                capability,
                action_label=f"call {tool_name}",
            )
        except RoleDenied as exc:
            return {
                "ok": False,
                "error": {
                    "code": "permission_denied",
                    "message": str(exc),
                },
            }

    # --- 2. Owner-scope (R5) for schedule-targeting tools -----------------
    if tool_name in _SCHEDULE_TARGETING:
        schedule_id = args.get("schedule_id")
        if schedule_id is not None:
            row = _fetch_schedule(ctx.db, schedule_id)
            if row is None:
                return {
                    "ok": False,
                    "error": {
                        "code": "schedule_not_found",
                        "message": f"schedule {schedule_id} not found",
                    },
                }
            if not can_access_schedule(row, ctx.caller_email,
                                       ctx.caller_role):
                return {
                    "ok": False,
                    "error": {
                        "code": "schedule_not_found",
                        "message": f"schedule {schedule_id} not found",
                    },
                }

    # --- 3. Run the tool body ---------------------------------------------
    body = TOOL_BODIES.get(tool_name)
    if body is None:
        return {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": f"no body registered for tool {tool_name}",
            },
        }
    return body(ctx, args)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fetch_schedule(db, schedule_id: int) -> dict | None:
    """Fetch a schedule row by id; return None if absent.

    Mirrors ``scheduler.schedules._fetch_row`` but returns None instead of
    raising KeyError, so dispatch can map both "doesn't exist" and
    "can't see it" to the same ``schedule_not_found`` code (R5).
    """
    row = db.execute(
        "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
    ).fetchone()
    if row is None:
        return None
    # Map to the same column order as scheduler.schedules._COLUMNS.
    columns = ("id", "owner", "source", "preset", "param", "fire_time",
               "enabled", "next_fire_at", "acl", "created_at", "updated_at")
    if hasattr(row, "keys"):
        return {col: row[col] for col in columns}
    return dict(zip(columns, row))
