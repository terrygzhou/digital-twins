"""MCP tool registry (feature 004, R2/R10, T006).

``build_tool_registry()`` returns the ten MCP tools as a list of dicts,
each with ``name``, ``description``, and ``inputSchema``.  The six real
scheduler tools mirror 003 ``contracts/scheduler.md`` field names exactly
(R2); the four BR-10 stubs carry a minimal ``inputSchema`` so a fresh
client sees a complete, stable registry (R10).

The 6 real tools:
    kb_schedule_list, kb_schedule_create, kb_schedule_update,
    kb_schedule_delete, kb_schedule_run, kb_run_history

The 4 BR-10 stubs:
    kb_search, kb_chat, kb_ingest, kb_health
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MCPContext:
    """The per-call context passed to every tool body and the dispatch
    executor.

    Attributes
    ----------
    db:
        Open state connection (accounts, schedules, audit_runs tables).
    caller_email:
        The authenticated caller's account email.
    caller_role:
        The caller's role (``admin`` / ``scheduler`` / ``reader``).
    agent_kind:
        The client's declared kind (e.g. ``"hermes"``,
        ``"claude-desktop"``) or ``"unknown"`` when absent.
    """
    db: Any
    caller_email: str
    caller_role: str
    agent_kind: str


# ---------------------------------------------------------------------------
# inputSchema builders (one per tool, mirroring 003 contracts)
# ---------------------------------------------------------------------------

def _agent_kind_prop() -> dict:
    return {
        "type": "string",
        "description":
            "The client's declared kind (e.g. 'hermes', 'claude-desktop'). "
            "Recorded on the audit row.",
    }


def _kb_schedule_list_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "all_users": {
                "type": "boolean",
                "default": False,
                "description":
                    "When true, list all users' schedules. Honored only "
                    "for an admin caller; a non-admin requesting it gets "
                    "permission_denied.",
            },
            "agent_kind": _agent_kind_prop(),
        },
        "required": [],
    }


def _kb_schedule_create_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "A 001 source name (e.g. 'hermes').",
            },
            "preset": {
                "type": "string",
                "enum": ["daily", "hourly", "weekly", "monthly",
                         "every-N-hours"],
                "description": "One of the five presets.",
            },
            "param": {
                "type": "integer",
                "description":
                    "Required iff preset='every-N-hours' (N >= 1). "
                    "Forbidden for all other presets.",
            },
            "fire_time": {
                "type": "string",
                "default": "03:00",
                "description": "HH:MM local time.",
            },
            "enabled": {
                "type": "boolean",
                "default": True,
                "description": "Whether the schedule is active.",
            },
            "acl": {
                "type": "string",
                "default": "owner",
                "description":
                    "Reserved ACL value; accepted and stored but not "
                    "enforced in v1.",
            },
            "agent_kind": _agent_kind_prop(),
        },
        "required": ["source", "preset"],
    }


def _kb_schedule_update_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "integer",
                "description": "The target schedule's id.",
            },
            "preset": {
                "type": "string",
                "enum": ["daily", "hourly", "weekly", "monthly",
                         "every-N-hours"],
                "description": "New preset (optional).",
            },
            "param": {
                "type": "integer",
                "description": "New param (optional).",
            },
            "fire_time": {
                "type": "string",
                "description": "New HH:MM fire time (optional).",
            },
            "enabled": {
                "type": "boolean",
                "description": "New enabled flag (optional).",
            },
            "acl": {
                "type": "string",
                "description": "New ACL value (optional).",
            },
            "agent_kind": _agent_kind_prop(),
        },
        "required": ["schedule_id"],
    }


def _kb_schedule_delete_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "integer",
                "description": "The target schedule's id.",
            },
            "agent_kind": _agent_kind_prop(),
        },
        "required": ["schedule_id"],
    }


def _kb_schedule_run_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "integer",
                "description": "The target schedule's id.",
            },
            "agent_kind": _agent_kind_prop(),
        },
        "required": ["schedule_id"],
    }


def _kb_run_history_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "user": {
                "type": "string",
                "description":
                    "Target account email. Omitted means the caller's own "
                    "runs. Set to another user's email for a cross-user "
                    "read (admin-only).",
            },
            "source": {
                "type": "string",
                "description": "Optional filter on the run's source.",
            },
            "since": {
                "type": "string",
                "description":
                    "Optional lower bound on started_at (UTC ISO-8601).",
            },
            "until": {
                "type": "string",
                "description":
                    "Optional upper bound on started_at (UTC ISO-8601).",
            },
            "limit": {
                "type": "integer",
                "default": 100,
                "description": "Max rows to return (default 100, max 1000).",
            },
            "agent_kind": _agent_kind_prop(),
        },
        "required": [],
    }


def _stub_schema() -> dict:
    """Minimal inputSchema for the four BR-10 stubs (R10)."""
    return {
        "type": "object",
        "properties": {
            "agent_kind": _agent_kind_prop(),
        },
        "required": [],
    }


# ---------------------------------------------------------------------------
# build_tool_registry
# ---------------------------------------------------------------------------

def build_tool_registry() -> list[dict]:
    """Return the ten MCP tools as a list of dicts.

    Each dict has:
    - ``name``: the tool name (stable, agent-facing).
    - ``description``: a human-readable summary.
    - ``inputSchema``: a JSON-Schema object describing the arguments.

    The six real tools mirror 003 ``contracts/scheduler.md`` field names
    exactly (R2).  The four BR-10 stubs carry a minimal schema (R10).
    """
    return [
        {
            "name": "kb_schedule_list",
            "description":
                "List schedules. Own scope by default; all_users=true is "
                "admin-only and returns every owner's schedules.",
            "inputSchema": _kb_schedule_list_schema(),
        },
        {
            "name": "kb_schedule_create",
            "description":
                "Create a schedule for the caller. owner is always the "
                "caller in v1 (caller-scoped, R5).",
            "inputSchema": _kb_schedule_create_schema(),
        },
        {
            "name": "kb_schedule_update",
            "description":
                "Update a schedule's cadence fields (preset, param, "
                "fire_time, enabled, acl). Owner-scoped.",
            "inputSchema": _kb_schedule_update_schema(),
        },
        {
            "name": "kb_schedule_delete",
            "description":
                "Delete a schedule. Owner-scoped.",
            "inputSchema": _kb_schedule_delete_schema(),
        },
        {
            "name": "kb_schedule_run",
            "description":
                "Trigger a pipeline run for the given schedule. The run is "
                "audited with trigger='mcp' and the caller's email as "
                "scheduled_by.",
            "inputSchema": _kb_schedule_run_schema(),
        },
        {
            "name": "kb_run_history",
            "description":
                "Query audit_runs history. Own scope by default; cross-user "
                "reads require the view_all_history capability (admin). "
                "Admin cross-user reads write one access-log row.",
            "inputSchema": _kb_run_history_schema(),
        },
        # --- BR-10 stubs (R10) ---
        {
            "name": "kb_search",
            "description":
                "BR-10 stub: knowledge-base search. Not implemented in "
                "004; returns not_implemented_yet.",
            "inputSchema": _stub_schema(),
        },
        {
            "name": "kb_chat",
            "description":
                "BR-10 stub: knowledge-base chat. Not implemented in "
                "004; returns not_implemented_yet.",
            "inputSchema": _stub_schema(),
        },
        {
            "name": "kb_ingest",
            "description":
                "BR-10 stub: knowledge-base ingest. Not implemented in "
                "004; returns not_implemented_yet.",
            "inputSchema": _stub_schema(),
        },
        {
            "name": "kb_health",
            "description":
                "BR-10 stub: knowledge-base health. Not implemented in "
                "004; returns not_implemented_yet.",
            "inputSchema": _stub_schema(),
        },
    ]
