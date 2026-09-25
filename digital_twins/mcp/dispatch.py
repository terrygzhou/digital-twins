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
import threading
import uuid
from typing import Any, Callable

from ..accounts import owner_tag_for, require_capability, RoleDenied
from ..config.schema import get as _cfg_get
from ..health import QDRANT_COLLECTION, qdrant_collection
from ..ingest.pipeline import run_pipeline  # noqa: F401 — monkeypatch seam
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
# tool bodies (Phase 2: the 004 scheduler bodies + kb_run_history)
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
                "qdrant.url is not set — run setup or set KB_QDRANT__URL")
        from qdrant_client import QdrantClient
        return QdrantClient(
            url=url, api_key=get(config, "qdrant.api_key") or None)

    return factory


def _resolve_neo4j_driver(config):
    """Lazy Neo4j driver resolver (S4 alignment, task 3.3).

    Mirrors ``_resolve_qdrant_factory``: a zero-arg factory so the
    driver is only constructed after the prerequisite check passes,
    and so tests can monkeypatch the module attribute.

    Returns ``None`` — never raises — when the knobs are unconfigured
    or construction fails: those cases mean the run proceeds
    Qdrant-only, matching ``run_pipeline``'s optional-neo4j semantics
    (the graph write is skipped and a warning is logged, not fatal).
    """
    import logging

    from ..config.schema import get

    url = get(config, "neo4j.url")
    user = get(config, "neo4j.user")
    password = get(config, "neo4j.password")
    if not (url and user and password):
        logging.warning(
            "kb_ingest: neo4j.url/user/password not fully configured "
            "— proceeding Qdrant-only (no graph writes)")
        return None

    def factory():
        from ..scheduler.loop import build_neo4j_driver
        driver = build_neo4j_driver(config)
        return driver

    try:
        return factory()
    except Exception as exc:
        logging.warning(
            "kb_ingest: Neo4j driver construction failed (%s) — "
            "proceeding Qdrant-only (no graph writes)", exc)
        return None


def _resolve_embedder(config):
    """Lazy embedder: the heavy model loads on first call, not at call start.

    Mirrors ``scheduler.loop._embedder``.  Tests monkeypatch this to a fixed
    384-dim stub so no model is loaded.  FR-003: endpoint-aware embedder when
    ``embedding.endpoint`` is set (additive, unchanged default).
    """
    from ..config.schema import get
    if get(config, "embedding.endpoint"):
        from ..ingest.embedding import build_endpoint_embedder
        return build_endpoint_embedder(config)

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


# ---------------------------------------------------------------------------
# 007 KB tool bodies (007-R1..R4) + shared helpers
# ---------------------------------------------------------------------------

#: The 006 503 remediation hint, verbatim (contracts/web-api.md).  ``kb_search``
#: maps any Qdrant failure (unconfigured / construction / transport) to this
#: string — never a traceback (007-R1d).
_QDRANT_UNAVAILABLE_REMEDIATION = (
    "check qdrant.url (env: KB_QDRANT__URL) points at a live Qdrant "
    "host:port, and that the collection exists")


class QdrantUnavailable(Exception):
    """Raised when the Qdrant client cannot be resolved or the call fails.

    The body maps this to the ``qdrant_unavailable`` error code + the exact
    006 remediation string (007-R1d).
    """


class EmbeddingUnavailable(Exception):
    """Raised when the embedding model cannot be loaded (007-R1e).

    The body maps this to the ``embedding_unavailable`` error code (a code
    distinct from ``qdrant_unavailable``), naming ``embedding.model``.
    """


def _fail_closed() -> dict:
    """The 007-R6e fail-closed guard: config is None → config_not_loaded."""
    return {
        "ok": False,
        "error": {
            "code": "config_not_loaded",
            "message": ("MCPContext.config is None; the transport must "
                        "load config before dispatch"),
        },
    }


# D-007-2: one client per distinct connection-param key, not one client per
# process.  A second MCPContext (or owner) with the same qdrant.url but a
# different qdrant.api_key must not silently share the first client.
_QDRANT_CLIENT_CACHE_KEY = "_qdrant_client_cache"
_qdrant_client_cache_lock = threading.Lock()


def _resolve_qdrant_client(config) -> "QdrantClient":
    """Resolve the Qdrant client from the config layer (006 ``_qdrant_client``
    pattern, web/app.py lines 383–412).

    Reads ``qdrant.url`` / ``qdrant.api_key``.  Unconfigured or a
    construction/transport failure raises :class:`QdrantUnavailable`, which
    the body maps to ``qdrant_unavailable`` + the exact 006 remediation.
    The client is cached on the module so repeated calls don't reconstruct
    it (007-R1: a pooled client, not a per-request client).

    D-007-2: the cache is keyed on the full tuple of config-derived
    connection params the constructor consumes (url + api_key — every knob
    the ``QdrantClient(...)`` call reads from config, so no silent sharing
    across configs that differ in any of them), and the check-then-set is
    guarded by ``_qdrant_client_cache_lock`` (atomic under concurrent first
    calls; the lock is the pooled-embedder pool-style module-global guard).
    """
    # NOTE: the QdrantUnavailable reference in this docstring is defined
    # below the imports (above the body that raises it).
    url = _cfg_get(config, "qdrant.url")
    if not url:
        raise QdrantUnavailable("qdrant.url is not configured")
    key = (url, _cfg_get(config, "qdrant.api_key") or None)
    cache = globals().get(_QDRANT_CLIENT_CACHE_KEY)
    if cache is None:
        cache = {}
        globals()[_QDRANT_CLIENT_CACHE_KEY] = cache
    with _qdrant_client_cache_lock:
        client = cache.get(key)
        if client is None:
            try:
                from qdrant_client import QdrantClient
                client = QdrantClient(url=url, api_key=key[1])
            except Exception as exc:  # construction/transport failure
                raise QdrantUnavailable(str(exc)) from exc
            cache[key] = client
    return client


def _embed_query(config, text: str):
    """Embed a single query string with the config-pinned model.

    Pooled: the heavy ``load_embedder`` model loads **once per process**
    (a lazy module-level cache keyed on model + device), not once per
    request (006's ``_pooled_embedder`` pattern, adapted to a module cache).
    A load failure raises :class:`EmbeddingUnavailable`, which the body maps
    to ``embedding_unavailable`` naming ``embedding.model`` (007-R1e).
    """
    from ..ingest.embedding import DEFAULT_MODEL

    model = _cfg_get(config, "embedding.model") or DEFAULT_MODEL
    device = _cfg_get(config, "embedding.device") or "auto"
    key = (model, device)
    pool = globals().get("_embed_pool")
    if pool is None:
        pool = {}
        globals()["_embed_pool"] = pool
    if key not in pool:
        try:
            from ..ingest.embedding import load_embedder
            pool[key] = load_embedder(model, device)
        except Exception as exc:  # load failure (download / unpinned / etc)
            raise EmbeddingUnavailable(
                f"embedding.model {model!r} failed to load: {exc}") from exc
    return pool[key].encode([text])


def _kb_search_body(ctx: MCPContext, args: dict) -> dict:
    """Owner-scoped Qdrant vector search (007-R1, mirrors 006's
    ``web/app.py::_handle_kb_search``).

    Order of operations:
      1. Fail-closed guard (config None → ``config_not_loaded``).
      2. Validate ``query`` (blank/missing/non-str →
         ``bad_request "query must be a non-empty string"`` — 006's exact
         400 message, **before** any Qdrant/embedding work).
      3. Clamp ``limit`` (default 5, cap 100; non-int → 5).
      4. ``owner_tag = owner_tag_for(ctx.caller_email)``.
      5. ``_resolve_qdrant_client(ctx.config)`` (006 ``_qdrant_client``
         pattern; unconfigured/construction/transport failure →
         ``qdrant_unavailable`` + the exact 006 remediation).
      6. Pooled ``_embed_query(ctx.config, text)`` (the heavy model loads
         once, not per request; load failure → ``embedding_unavailable``
         naming ``embedding.model``).
      7. ``query_points(qdrant_collection(ctx.config), query=<vec>,
         query_filter=Filter(must=[FieldCondition(key='owner_tag',
         match=MatchValue(value=owner_tag))]), limit=limit,
         with_payload=True)`` — the 006 filter verbatim.
      8. Map rows to ``{score, source_url, text, source, chunk_index}``,
         sort descending by score, return ``{"ok": True, "results": [...],
         "count": N}``.
    """
    if ctx.config is None:
        return _fail_closed()

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {
            "ok": False,
            "error": {
                "code": "bad_request",
                "message": "query must be a non-empty string",
            },
        }

    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 100))

    owner_tag = owner_tag_for(ctx.caller_email)

    try:
        client = _resolve_qdrant_client(ctx.config)
    except Exception:
        # Any qdrant-resolution failure (unconfigured URL, transport down,
        # construction error) → the clean qdrant_unavailable hint, never a
        # traceback (007-R1d).
        return {
            "ok": False,
            "error": {
                "code": "qdrant_unavailable",
                "remediation": _QDRANT_UNAVAILABLE_REMEDIATION,
            },
        }

    try:
        embedding = _embed_query(ctx.config, query)
        # _embed_query returns the encode() result; the first (only) vector
        # is the query vector.  Handle both a batch-with-``tolist`` and a
        # plain list (test fakes may return either).
        if hasattr(embedding, "tolist"):
            vectors = embedding.tolist()
        else:
            vectors = list(embedding)
        if not vectors:
            raise QdrantUnavailable("no embedding produced for the query")
        query_vector = vectors[0]
    except EmbeddingUnavailable:
        return {
            "ok": False,
            "error": {
                "code": "embedding_unavailable",
                "remediation": (
                    "check embedding.model / embedding.device point at a "
                    "loadable model (embedding.model failed to load)"
                ),
            },
        }
    except Exception:
        # Any other embedding failure → the same clean code (never a
        # traceback; the distinct code from qdrant_unavailable, 007-R1e).
        return {
            "ok": False,
            "error": {
                "code": "embedding_unavailable",
                "remediation": (
                    "check embedding.model / embedding.device point at a "
                    "loadable model (embedding.model failed to load)"
                ),
            },
        }

    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        coll = qdrant_collection(ctx.config)
        results = client.query_points(
            coll,
            query=query_vector,
            query_filter=Filter(must=[FieldCondition(
                key="owner_tag", match=MatchValue(value=owner_tag))]),
            limit=limit,
            with_payload=True,
        )
    except Exception:
        # query_points call failure / transport down: the clean
        # qdrant_unavailable hint — never a traceback (007-R1d).
        return {
            "ok": False,
            "error": {
                "code": "qdrant_unavailable",
                "remediation": _QDRANT_UNAVAILABLE_REMEDIATION,
            },
        }

    # The real qdrant-client ``query_points`` returns a ``QueryResponse``
    # (points under ``.points``); test fakes may return a bare list.
    points = results.points if hasattr(results, "points") else results
    payload_rows = [
        {
            "score": r.score,
            "source_url": (r.payload or {}).get("source_url"),
            "text": (r.payload or {}).get("text"),
            "full_content": (r.payload or {}).get("full_content"),
            "content_snippet": (r.payload or {}).get("content_snippet"),
            "source": (r.payload or {}).get("source"),
            "chunk_index": (r.payload or {}).get("chunk_index"),
            "item_id": (r.payload or {}).get("item_id"),
            "id": r.id,
        }
        for r in points
    ]
    rows = sorted(payload_rows,
                  key=lambda row: row["score"] or 0.0, reverse=True)
    if args.get("expand"):
        rows = _attach_graph_expansion(ctx, rows)
    return {"ok": True, "results": rows, "count": len(rows)}


def _attach_graph_expansion(ctx, rows: list) -> list:
    """Attach graph-relative sibling chunks to search results.

    Only invoked when the caller opts in (``args["expand"]``).  When
    Neo4j is not configured (no ``neo4j.url`` in the config) the
    function returns the rows unchanged — the Qdrant-only path is
    unaffected.  When configured, each hit's ``id`` (the deterministic
    ``point_id``) is used to query the graph for sibling chunks under
    the same channel in the S4 ``:SourceItem`` graph; the results are
    attached as a ``"related"`` list on each row.
    """
    from ..ingest import graph_query
    from ..config.schema import get as cfg_get
    url = cfg_get(ctx.config, "neo4j.url")
    if not url:
        return rows  # neo4j not configured — skip graph expansion
    try:
        from ..scheduler.loop import build_neo4j_driver
        driver = build_neo4j_driver(ctx.config)
    except Exception:
        return rows  # driver construction failed — Qdrant-only result

    try:
        for row in rows:
            item_id = (row.get("payload") or {}).get("item_id") or row.get("item_id")
            if not item_id:
                continue
            row["related"] = graph_query.expand_relatives(driver, item_id)
        return rows
    finally:
        driver.close()


def _kb_chat_body(ctx: MCPContext, args: dict) -> dict:
    """007-R2: kb_chat — "surface only" in 007.

    Mirrors 006's two-branch ``_handle_chat``: the body reads
    ``llm.endpoint`` / ``llm.model`` (the only knob access — makes the
    body decision-ready for the follow-up slice that fills generation)
    and returns the 501-surface result in BOTH branches, whether the
    knobs are set or unset.

    Steps (007 plan "kb_chat body"):

    1. ``ctx.config is None`` → ``config_not_loaded`` (006
       ``_fail_closed`` guard; no other work).
    2. ``query`` missing / blank / non-string → ``bad_request``
       ``"query must be a non-empty string"`` — BEFORE any config read
       (the 006 message verbatim; the body short-circuits on the query
       check so no llm.* knob is touched).
    3. Read ``get(cfg, "llm.endpoint")`` + ``get(cfg, "llm.model")``
       (the only knob access; the read is what makes the body
       decision-ready — a follow-up slice can branch on whether the
       knobs are set to decide whether to call the LLM).
    4. Return the 501-surface result in BOTH branches (007-R2):
       ``{"ok": False, "error": {"code": "not_implemented",
       "remediation": "set llm.endpoint / llm.model to enable chat
       (007 ships the surface only; follow-up slice fills generation)"}}``.
       No LLM call, no embedding, no Qdrant, no network.
    """
    if ctx.config is None:
        return _fail_closed()

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {
            "ok": False,
            "error": {
                "code": "bad_request",
                "message": "query must be a non-empty string",
            },
        }

    # The only knob access — makes the body decision-ready for the
    # follow-up slice that fills generation (007-R2).  007 ships the
    # surface only: the read happens, the LLM call does not.
    _ = _cfg_get(ctx.config, "llm.endpoint")
    _ = _cfg_get(ctx.config, "llm.model")

    # Both branches return the same 501-surface result — mirroring
    # 006's two-branch handler (set → would-call-LLM branch; unset →
    # not-implemented branch; in 007 both return the surface result).
    return {
        "ok": False,
        "error": {
            "code": "not_implemented",
            "remediation": (
                "set llm.endpoint / llm.model to enable chat "
                "(007 ships the surface only; follow-up slice fills "
                "generation)"
            ),
        },
    }


def _kb_ingest_body(ctx: MCPContext, args: dict) -> dict:
    """Trigger a pipeline run for the caller (007-R3).

    Mirrors 006's ``web/app.py::_handle_ingest_run`` order — the 007 plan
    "kb_ingest body":

    1. Fail-closed guard (``ctx.config is None`` → ``config_not_loaded``;
       no other work).
    2. Capability gate FIRST: ``require_capability(ctx.caller_role,
       "trigger_run", "trigger a run")`` — the body's own gate, mirroring
       the ``dispatch()`` gate but at the body level so the body's tests
       can assert it in isolation.  Refusal → ``permission_denied``
       naming ``trigger_run`` (the RoleDenied message includes the
       capability id); NO ``run_pipeline`` call, NO audit row.
    3. Source validation (exact 006 messages, all ``bad_request``,
       zero pipeline calls):
         * ``source`` missing / ``"all"`` with no enabled sources →
           ``"no sources enabled"``.
         * ``source = <name>`` unknown → ``"unknown source '<name>'"``.
         * ``source = <name>`` not in ``BUILTIN_SOURCES`` →
           ``"unknown source '<name>'"``.
         * ``source = <name>`` in ``BUILTIN_SOURCES`` but disabled →
           ``"source '<name>' is not enabled"``.
       The caller's enabled-source set comes from
       ``ctx.config.get("sources", {})`` — the 006 web reads the same
       nested dict (built-in source names are known from
       ``schema.BUILTIN_SOURCES``; custom names are looked up in the
       ``sources`` dict, mirroring 006's two-branch source validation).
    4. ``merged_cfg`` = schema defaults merged over ``ctx.config``
       (006's ``_merge_defaults`` equivalent: nested dict from the flat
       dotted-key DEFAULTS, caller wins).
    5. Resolve the qdrant factory + embedder via Phase-3 helpers
       (``_resolve_qdrant_factory`` / ``_resolve_embedder`` — the same
       lazy, monkeypatchable helpers the 004 ``kb_schedule_run`` body
       uses).
    6. ``run_pipeline(merged_cfg, ctx.db, qdrant_factory, embedder,
       source_names=..., trigger="mcp", scheduled_by=caller_email,
       owner=caller_email)`` — resolved at call time via the module
       attribute (the 006 ``_pipeline_mod.run_pipeline`` monkeypatch
       seam).  **NO ``agent_kind`` kwarg** (run_pipeline's signature
       has no ``agent_kind`` parameter, ingest/pipeline.py:105).
       ``neo4j`` is left at its default (Qdrant-only).
    7. Post-call: ``_stamp_agent_kind(ctx.db, summary.run_id,
       ctx.agent_kind)`` — the 004 D6 pattern, mirroring
       ``_kb_schedule_run_body`` step 5.  This is how ``agent_kind`` is
       recorded on the audit row (007-R3d/BR-11.5.3); the pipeline
       itself does NOT write it.
    8. Error mapping:
         * ``PrerequisiteError`` (``exc.source`` + ``exc.missing``) →
           ``prerequisite_missing`` with the message
           ``"source '<name>': missing prerequisite(s): <missing>"``.
           The pipeline writes its own ``failed`` audit row; the body
           writes NO second row.
         * ``UnknownSourceError`` (``exc.args[0]`` = source name) →
           ``bad_request`` with ``"unknown source '<name>'"``.
         * Other exceptions → ``run_failed`` with ``str(exc)``; the
           pipeline writes its own ``failed`` audit row.
    9. Success → ``{"ok": True, "run_id", "status", "counts", "points"}``
       — the 006 web result shape.
    """
    from ..config.schema import BUILTIN_SOURCES
    from ..ingest import pipeline as _pipeline_mod
    from ..sources import UnknownSourceError

    # 1. Fail-closed guard (006 _fail_closed).
    if ctx.config is None:
        return _fail_closed()

    # 2. Capability gate FIRST — the body's own gate, mirroring the
    #    dispatch() gate but at the body level.  Refusal →
    #    permission_denied naming trigger_run; NO run_pipeline call,
    #    NO audit row (007-R3b).
    try:
        require_capability(
            ctx.caller_role, "trigger_run", "trigger a run")
    except RoleDenied as exc:
        return {
            "ok": False,
            "error": {
                "code": "permission_denied",
                "message": str(exc),
            },
        }

    # 3. Source validation (exact 006 messages, all bad_request).
    sources_cfg = (ctx.config or {}).get("sources") or {}
    enabled = [
        name for name, entry in sources_cfg.items()
        if isinstance(entry, dict) and entry.get("enabled")
    ]
    source = args.get("source")
    if source in (None, "", "all"):
        if not enabled:
            return {
                "ok": False,
                "error": {
                    "code": "bad_request",
                    "message": "no sources enabled",
                },
            }
        source_names = enabled
    else:
        # A specific source name.  Unknown / disabled → bad_request with
        # the exact 006 message.
        if source not in BUILTIN_SOURCES and source not in sources_cfg:
            return {
                "ok": False,
                "error": {
                    "code": "bad_request",
                    "message": f"unknown source '{source}'",
                },
            }
        entry = sources_cfg.get(source)
        if not (isinstance(entry, dict) and entry.get("enabled")):
            return {
                "ok": False,
                "error": {
                    "code": "bad_request",
                    "message": f"source '{source}' is not enabled",
                },
            }
        source_names = [source]

    # 4. merged_cfg = schema defaults merged over ctx.config (006
    #    _merge_defaults equivalent, D-007-3 fix): the nested dict
    #    built from the flat dotted-key DEFAULTS, with the caller's
    #    config deep-merged on top (caller wins per key; sibling
    #    defaults under a partially-overridden subtree are preserved
    #    — the pre-fix flat dotted-key overlay dropped them).
    merged_cfg = _deep_merge(
        _defaults_as_nested(_pipeline_mod_run_defaults()),
        ctx.config or {},
    )

    # 5. Resolve the qdrant factory + embedder (the same lazy,
    #    monkeypatchable helpers the 004 kb_schedule_run body uses).
    qdrant_factory = _resolve_qdrant_factory(merged_cfg)
    embedder = _resolve_embedder(merged_cfg)

    # 6. run_pipeline hand-off — resolved at call time via the
    #    dispatch module attribute (the 006 _pipeline_mod.run_pipeline
    #    monkeypatch seam — the test monkeypatches dispatch.run_pipeline,
    #    which is what the body reads).  NO agent_kind kwarg
    #    (run_pipeline's signature has none, ingest/pipeline.py:105).
    #    S4 alignment (task 3.3): the Neo4j driver is resolved from
    #    the config and passed as ``neo4j=`` so that every ingest
    #    surface writes the graph, not just CLI/schedule.  When
    #    unconfigured or unconstructable, the resolver returns None
    #    and the run proceeds Qdrant-only (logged, not fatal).
    neo4j_driver = _resolve_neo4j_driver(merged_cfg)
    run_pipeline = globals().get("run_pipeline", _pipeline_mod.run_pipeline)
    try:
        summary = run_pipeline(
            merged_cfg, ctx.db, qdrant_factory, embedder, neo4j_driver,
            source_names=source_names,
            trigger="mcp",
            scheduled_by=ctx.caller_email,
            owner=ctx.caller_email,
        )
    except _pipeline_mod.PrerequisiteError as exc:
        # The pipeline writes its own `failed` audit row on the
        # exception path (start_audit_run up front + finish on except).
        # Map to the stable code; the row is audited `failed` with
        # trigger='mcp'.  The body writes NO second row.
        return {
            "ok": False,
            "error": {
                "code": "prerequisite_missing",
                "message": (
                    f"source '{exc.source}': missing prerequisite(s): "
                    f"{'; '.join(exc.missing)}"
                ),
            },
        }
    except UnknownSourceError as exc:
        # 006 web pattern: the source name is exc.args[0] (KeyError
        # subclass, the 001 pipeline raises with the source name as the
        # single arg).
        name = exc.args[0] if exc.args else str(exc)
        return {
            "ok": False,
            "error": {
                "code": "bad_request",
                "message": f"unknown source '{name}'",
            },
        }
    except Exception as exc:
        # The pipeline writes its own `failed` audit row on the
        # exception path.  Map to the stable run_failed code; the
        # row is audited `failed` with trigger='mcp'.  The body
        # writes NO second row.
        return {
            "ok": False,
            "error": {
                "code": "run_failed",
                "message": str(exc),
            },
        }

    # 7. Post-call: stamp agent_kind on the audit row's
    #    per_source_counts JSON (004 D6 pattern, mirror
    #    _kb_schedule_run_body step 5).  This is how agent_kind is
    #    recorded (007-R3d/BR-11.5.3); the pipeline does NOT write it.
    _stamp_agent_kind(ctx.db, summary.run_id, ctx.agent_kind)

    # 8/9. Success — the 006 web result shape.
    return {
        "ok": True,
        "run_id": summary.run_id,
        "status": summary.status,
        "counts": summary.counts,
        "points": summary.points,
    }


def _pipeline_mod_run_defaults() -> dict:
    """The flat dotted-key DEFAULTS from ``config.schema``.

    The 006 web's ``_merge_defaults`` builds a nested dict from this
    flat mapping.  This helper returns the flat mapping so the body can
    apply the same "caller wins" overlay without duplicating the
    DEFAULTS table.
    """
    from ..config.schema import DEFAULTS
    return dict(DEFAULTS)


def _defaults_as_nested(flat_defaults: dict) -> dict:
    """Expand a flat dotted-key mapping into a nested dict (006
    ``web/app.py::_merge_defaults`` expansion step, duplicated here —
    ``digital_twins/web/`` is a separate distribution layer and must
    not be imported from ``mcp/``).

    ``"chunking.max_chars": 800`` becomes ``{"chunking":
    {"max_chars": 800}}``.  ``None`` values are kept verbatim (the
    schema's "optional / unset" sentinel).  Returns a new dict tree.
    """
    nested: dict = {}
    for dotted_key, value in flat_defaults.items():
        parts = dotted_key.split(".")
        node = nested
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
    return nested


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge (006 ``web/app.py::_merge_defaults``
    overlay step, duplicated here): ``override`` wins per key; nested
    dicts merge recursively; any non-dict value (including ``None``)
    replaces the base value wholesale.

    Neither input is mutated — a new dict tree is built, so the
    schema defaults and the caller's config are both left untouched.
    (006's overlay is one level deep; this recurses, which matches
    006 for the schema's two-level DEFAULTS and mirrors 006's stated
    semantics — no other 006 subtlety, e.g. no None-skipping.)
    """
    merged: dict = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _kb_health_body(ctx: MCPContext, args: dict) -> dict:
    """Run the package's health checks (007-R4, mirrors 006's
    ``_handle_health``).

    Steps (007 plan "kb_health body"):

    1. Fail-closed guard (``ctx.config is None`` → ``config_not_loaded``;
       no other work).
    2. ``checks = _health_mod.run_health_checks(ctx.config)`` — the
       module-attribute seam (006's ``_health_mod`` pattern): the test
       monkeypatches ``digital_twins.health.run_health_checks`` so the
       body's read of ``_health_mod.run_health_checks`` sees the fake.
       No other I/O: no qdrant / embedding / network call (the
       function does all the I/O internally; the body just maps the
       results).
    3. Map each ``HealthResult`` to ``{endpoint, ok, detail,
       remediation}`` → ``{"ok": True, "checks": [...]}`` — field
       shape and order preserved (007-R4a).
    """
    # 1. Fail-closed guard (006 _fail_closed).
    if ctx.config is None:
        return _fail_closed()

    # 2. The module-attribute seam — the test monkeypatches
    #    digital_twins.health.run_health_checks so this read sees the
    #    fake.  No other I/O: no qdrant / embedding / network call
    #    (run_health_checks does all the I/O internally; the body just
    #    maps the results).
    from .. import health as _health_mod
    checks = _health_mod.run_health_checks(ctx.config)

    # 3. Map each HealthResult to the wire shape.
    return {
        "ok": True,
        "checks": [
            {
                "endpoint": r.endpoint,
                "ok": r.ok,
                "detail": r.detail,
                "remediation": r.remediation,
            }
            for r in checks
        ],
    }


TOOL_BODIES: dict[str, Callable[..., dict]] = {
    "kb_schedule_list": _kb_schedule_list_body,
    "kb_schedule_create": _kb_schedule_create_body,
    "kb_schedule_update": _kb_schedule_update_body,
    "kb_schedule_delete": _kb_schedule_delete_body,
    "kb_schedule_run": _kb_schedule_run_body,
    "kb_run_history": _kb_run_history_body,
    # 007 KB tool bodies (the BR-10 stubs are being replaced one by one)
    "kb_search": _kb_search_body,
    "kb_chat": _kb_chat_body,
    "kb_ingest": _kb_ingest_body,
    "kb_health": _kb_health_body,
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
