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

import json
import uuid
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


def _kb_schedule_update_body(ctx: MCPContext, args: dict) -> dict:
    """Update a schedule's fields (owner-scoped via dispatch, R5/R9).

    Delegates to ``scheduler.schedules.update_schedule`` (002 helper).
    The owner-scope check was already applied by dispatch (R5).

    Args: ``schedule_id`` (int, required) + any of ``preset``, ``param``,
    ``fire_time``, ``enabled``, ``acl``.

    R11: no audit_runs row (a CRUD mutation is not a run).
    """
    from ..scheduler.schedules import update_schedule

    schedule_id = args["schedule_id"]
    fields: dict[str, Any] = {}
    for key in ("preset", "param", "fire_time", "enabled", "acl"):
        if key in args:
            fields[key] = args[key]

    try:
        schedule = update_schedule(ctx.db, schedule_id, **fields)
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


def _kb_schedule_delete_body(ctx: MCPContext, args: dict) -> dict:
    """Delete a schedule (owner-scoped via dispatch, R5/R9).

    Delegates to ``scheduler.schedules.delete_schedule`` (002 helper).
    The owner-scope check was already applied by dispatch (R5).

    Args: ``schedule_id`` (int, required).

    R11: no audit_runs row (a CRUD mutation is not a run).
    """
    from ..scheduler.schedules import delete_schedule

    schedule_id = args["schedule_id"]
    delete_schedule(ctx.db, schedule_id)

    return {"ok": True, "data": {"deleted": schedule_id}}


# ---------------------------------------------------------------------------
# kb_schedule_run (T019/T020 — pipeline parity, research D4)
# ---------------------------------------------------------------------------

def _resolve_mcp_run_config(db):
    """Resolve the global config for an MCP-triggered run.

    Mirrors the 002/003 wiring: the tick (``serve_once_tick``) and the CLI
    ``run`` command each load the four-layer config and hand the *resolved*
    config to ``run_pipeline``.  The MCP run body does the same.

    Tests monkeypatch this hook (it is the single seam that reaches the
    process environment) so the unit tests can hand a controlled config
    without touching the real env / config files.
    """
    from ..config.loader import load
    return load()


def _resolve_qdrant_factory(config):
    """Zero-arg factory returning the configured Qdrant client (lazy).

    Mirrors ``scheduler.loop._qdrant_factory``: the client is only
    constructed after the prerequisite check passes.  Tests monkeypatch this
    to hand back an in-memory client.
    """
    from ..config.schema import get

    url = get(config, "qdrant.url")

    def factory():
        if not url:
            from ..config.schema import SchemaError
            raise SchemaError(
                "qdrant.url is not set — run init or set KB_QDRANT__URL")
        from qdrant_client import QdrantClient
        return QdrantClient(
            url=url, api_key=get(config, "qdrant.api_key") or None)

    return factory


def _resolve_embedder(config):
    """Lazy embedder: the heavy model loads on first call, not at call start.

    Mirrors ``scheduler.loop._embedder``.  Tests monkeypatch this to a fixed
    384-dim stub so no model is loaded.
    """
    from ..config.schema import get
    state = {}

    def embed(texts):
        if "model" not in state:
            from ..ingest.embedding import load_embedder
            state["model"] = load_embedder(
                get(config, "embedding.model"),
                get(config, "embedding.device") or "auto")
        return state["model"].encode(list(texts)).tolist()

    return embed


def _stamp_agent_kind(db, run_id: str, agent_kind: str) -> None:
    """Record ``agent_kind`` on the run's audit row ``per_source_counts`` (D6).

    The pipeline owns the audit row (``finish_audit_run`` writes the real
    per-source counts).  This helper merges ``{"agent_kind": <kind>}`` into
    that JSON in the same transaction window so the row is queryable
    (constitution V) without a schema change.  No-op if the row is absent.
    """
    row = db.execute(
        "SELECT per_source_counts FROM audit_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return
    counts_json = row[0]
    try:
        counts = json.loads(counts_json) if counts_json else {}
    except (ValueError, TypeError):
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    counts["agent_kind"] = agent_kind
    db.execute(
        "UPDATE audit_runs SET per_source_counts=? WHERE run_id=?",
        (json.dumps(counts), run_id),
    )
    db.commit()


def _kb_schedule_run_body(ctx: MCPContext, args: dict) -> dict:
    """Trigger a pipeline run for the target schedule (research D4).

    Owner-scoping was already applied by dispatch (R5 → schedule_not_found).
    The body:
      1. Re-fetches the schedule (the caller may be admin running another
         user's schedule).
      2. If the schedule's source is disabled → ``source_disabled`` (nothing
         to run; no audit row).
      3. ``merged = merge_user_config(config, db, owner, source=...)`` (003).
      4. ``run_pipeline(merged, db, qdrant, embedder,
         source_names=[source], trigger="mcp",
         scheduled_by=caller_email, owner=owner)`` (001 — the SAME pipeline
         every trigger path uses; one-record-not-N holds).
      5. Stamps ``agent_kind`` onto that run's ``per_source_counts`` (D6).
      6. Returns the run record (R2 field names).

    One pipeline-owned ``audit_runs`` row: ``trigger='mcp'``,
    ``scheduled_by=<caller>``, ``status`` = pipeline outcome.
    """
    import json
    from ..ingest.pipeline import run_pipeline
    from ..user_config import merge_user_config

    schedule_id = args["schedule_id"]
    schedule = _fetch_schedule(ctx.db, schedule_id)
    if schedule is None:
        return {
            "ok": False,
            "error": {
                "code": "schedule_not_found",
                "message": f"schedule {schedule_id} not found",
            },
        }

    source = schedule["source"]
    owner = schedule["owner"]

    # Resolve the config and check the source is enabled (fail-fast, mirroring
    # the 002 tick's paused-source guard: nothing to run).
    config = _resolve_mcp_run_config(ctx.db)
    entry = (config.get("sources") or {}).get(source) or {}
    if not entry.get("enabled"):
        return {
            "ok": False,
            "error": {
                "code": "source_disabled",
                "message": (
                    f"source '{source}' is not enabled — re-enable it or "
                    f"point the schedule at an enabled source"
                ),
            },
        }

    qdrant = _resolve_qdrant_factory(config)
    embedder = _resolve_embedder(config)
    merged = merge_user_config(config, ctx.db, owner, source=source)

    try:
        summary = run_pipeline(
            merged, ctx.db, qdrant, embedder,
            source_names=[source],
            trigger="mcp",
            scheduled_by=ctx.caller_email,
            owner=owner,
        )
    except Exception as exc:
        # The pipeline writes its own `failed` audit row on the exception
        # path (start_audit_run up front + finish on except).  Map to the
        # stable error code; the row is audited `failed` with trigger='mcp'.
        return {
            "ok": False,
            "error": {
                "code": "run_failed",
                "message": str(exc),
            },
        }

    # D4/D6: stamp agent_kind onto the run's per_source_counts JSON.
    _stamp_agent_kind(ctx.db, summary.run_id, ctx.agent_kind)

    # A pipeline that ran but came back `failed` (e.g. a source error after
    # the fail-fast prerequisite check) maps to the stable `run_failed` code.
    # The row is already audited `failed` by the pipeline (trigger='mcp').
    if summary.status not in ("ok", "partial"):
        return {
            "ok": False,
            "error": {
                "code": "run_failed",
                "message": (
                    f"pipeline run {summary.run_id} finished with status "
                    f"{summary.status!r}"
                ),
            },
        }

    # Read the final per_source_counts back from the audited row so the
    # response reflects the real counts + the stamped agent_kind.
    row = ctx.db.execute(
        "SELECT per_source_counts FROM audit_runs WHERE run_id=?",
        (summary.run_id,),
    ).fetchone()
    per_source_counts = {}
    if row is not None and row[0]:
        try:
            per_source_counts = json.loads(row[0])
        except (ValueError, TypeError):
            per_source_counts = {}

    return {
        "ok": True,
        "data": {
            "run_id": summary.run_id,
            "status": summary.status,
            "per_source_counts": per_source_counts,
            "agent_kind": ctx.agent_kind,
        },
    }


# ---------------------------------------------------------------------------
# kb_run_history (T021/T022 — R8 access-log, R13 exclusion)
# ---------------------------------------------------------------------------

def _write_history_access_log(db, admin_email: str, target_user: str,
                              agent_kind: str) -> None:
    """Write the R8 access-log row for an admin cross-user history read.

    One read = one ``audit_runs`` row (an access-log event, not a run):
    ``run_id`` = fresh ``uuid.uuid4()``, ``status='ok'``, ``trigger='mcp'``,
    ``scheduled_by=<admin>``, ``per_source_counts`` =
    ``{"mcp_history_query": {"target_user": ..., "agent_kind": ...}}``.
    """
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, "
        "trigger, scheduled_by, per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, _now_iso(), _now_iso(), "ok", "mcp", admin_email,
         json.dumps({"mcp_history_query": {
             "target_user": target_user,
             "agent_kind": agent_kind,
         }})),
    )
    db.commit()


def _now_iso() -> str:
    """Aware-UTC ISO-8601 timestamp, matching 001's ``models._now()``."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kb_run_history_body(ctx: MCPContext, args: dict) -> dict:
    """Query audit_runs history (004 contract, tool 6).

    Own scope (``user`` omitted or ``user == caller``): ``list_runs(db,
    user=caller_email)`` filtered by ``source``/``since``/``until``/``limit``,
    R13-excluding any ``mcp_history_query`` row, and **no** audit row.

    Admin cross-user (``user != caller``, ``caller_role='admin'``):
    ``list_runs(db, user=<target>, all_users=True)`` + **one** R8 access-log
    row.  A non-admin cross-user request → ``permission_denied``.
    """
    from ..state.models import list_runs

    target = args.get("user")
    source = args.get("source")
    since = args.get("since")
    until = args.get("until")
    limit = args.get("limit", 100)
    limit = max(0, min(int(limit), 1000))

    cross_user = target is not None and target != ctx.caller_email
    own_scope = not cross_user

    if cross_user:
        # R4/R3: cross-user history requires view_all_history (admin-only).
        try:
            require_capability(
                ctx.caller_role,
                "view_all_history",
                action_label="read another user's run history",
            )
        except RoleDenied as exc:
            return {
                "ok": False,
                "error": {
                    "code": "permission_denied",
                    "message": str(exc),
                },
            }
        # R8: write the one access-log row before returning the data.
        _write_history_access_log(ctx.db, ctx.caller_email, target,
                                  ctx.agent_kind)
        # list_runs(all_users=True) performs its own role gate on the
        # *caller's* email (the admin).  We pass the admin's email so the
        # capability check is done against the actor (the target's role is
        # irrelevant to whether the read is allowed).  The `user=` target is
        # applied by the caller-side filtering below (we query all rows and
        # filter by target's scheduled_by to keep the cross-user scope
        # explicit without relying on a `user` param the query doesn't use).
        rows = list_runs(ctx.db, user=ctx.caller_email, all_users=True)
        # Filter to the target user's runs (the cross-user scope): the target
        # is `scheduled_by` their own runs.  Keep only rows scheduled_by the
        # target.
        rows = [r for r in rows if r[5] == target]
    else:
        rows = list_runs(ctx.db, user=ctx.caller_email)

    # R2 field names on each record.
    records = []
    for row in rows:
        (run_id, started_at, completed_at, status, trigger,
         scheduled_by, per_source_counts_json) = row
        # R13: exclude access-log rows (per_source_counts carries the
        # mcp_history_query key) so they are never mistaken for a run.
        counts = _parse_counts(per_source_counts_json)
        if "mcp_history_query" in counts:
            continue
        if source is not None and source not in counts:
            continue
        if since is not None and (started_at or "") < str(since):
            continue
        if until is not None and (started_at or "") > str(until):
            continue
        records.append({
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
            "trigger": trigger,
            "scheduled_by": scheduled_by,
            "per_source_counts": counts,
        })

    records = records[:limit]
    return {"ok": True, "data": {"runs": records, "count": len(records)}}


def _parse_counts(per_source_counts_json) -> dict:
    """Parse the audit row's per_source_counts JSON into a dict ({} on
    malformed/absent)."""
    if not per_source_counts_json:
        return {}
    try:
        parsed = json.loads(per_source_counts_json)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


TOOL_BODIES: dict[str, Callable[..., dict]] = {
    "kb_schedule_list": _kb_schedule_list_body,
    "kb_schedule_create": _kb_schedule_create_body,
    "kb_schedule_update": _kb_schedule_update_body,
    "kb_schedule_delete": _kb_schedule_delete_body,
    "kb_schedule_run": _kb_schedule_run_body,
    "kb_run_history": _kb_run_history_body,
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
