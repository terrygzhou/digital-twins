"""Schedule CRUD over the 001 state store (002 contracts/scheduler.md).

CRUD only, no fire logic. The schedules table lands in migration v2 (T004).

Time handling (data-model.md + T005 ruling):
    The database stores AWARE-UTC ISO-8601 strings for ``created_at``,
    ``updated_at`` and ``next_fire_at`` (seconds precision, matching 001's
    ``models._now()``). ``expand_next`` (T003) is a pure local-time function
    and works on naive wall-clock datetimes, so this module converts to naive
    UTC **only at the expand_next boundary** and re-wraps the result back to
    aware-UTC before storing. The stored strings are therefore always in one
    consistent format and compare lexicographically in the same order they
    compare as datetimes (all ``+00:00``, same width).

Functions:
    create_schedule, list_schedules, update_schedule, delete_schedule,
    due_schedules, claim_and_advance.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from digital_twins.scheduler.presets import expand_next, preset_values

_VALID_PRESETS = tuple(preset_values())

_COLUMNS = ("id", "owner", "source", "preset", "param", "fire_time",
            "enabled", "next_fire_at", "acl", "created_at", "updated_at")


# --- time helpers (module-private) -----------------------------------------

def _now_utc() -> datetime:
    """Actual current time, aware-UTC."""
    return datetime.now(timezone.utc)


def _to_aware_utc(dt: datetime) -> datetime:
    """Coerce a naive-or-aware datetime to aware-UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_naive_utc(dt: datetime) -> datetime:
    """Strip tzinfo from an aware-UTC datetime -> naive UTC (for expand_next)."""
    aware = _to_aware_utc(dt)
    return aware.replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    """Render a datetime as aware-UTC ISO-8601 (seconds), 001-consistent."""
    return _to_aware_utc(dt).isoformat(timespec="seconds")


def _parse_iso(stored: str) -> datetime:
    """Parse a stored ISO-8601 string to a (possibly naive) datetime."""
    return datetime.fromisoformat(stored)


def _row_to_dict(row) -> dict:
    """Map a schedules row (sqlite3.Row or tuple, in _COLUMNS order) to the
    contract's dict shape."""
    if isinstance(row, sqlite3.Row):
        return {col: row[col] for col in _COLUMNS}
    return dict(zip(_COLUMNS, row))


def _fetch_row(db, schedule_id: int) -> dict:
    """Fetch one schedule row by id as a dict; raise KeyError if absent."""
    row = db.execute(
        "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"schedule id {schedule_id} not found")
    return _row_to_dict(row)


def _validate_preset_param(preset: str, param) -> None:
    """Fail-fast preset/param validation, mirroring expand_next's rules.

    - preset must be in the 5-value set.
    - every-N-hours requires an int param >= 1.
    - every other preset rejects a param.
    """
    if preset not in _VALID_PRESETS:
        raise ValueError(f"unknown preset: {preset!r}")
    if preset != "every-N-hours" and param is not None:
        raise ValueError(
            f"param is only valid for 'every-N-hours', not {preset!r}"
        )
    if preset == "every-N-hours":
        if (not isinstance(param, int) or isinstance(param, bool)
                or param < 1):
            raise ValueError(
                f"every-N-hours requires param to be an int >= 1, got {param!r}"
            )


# --- public API -------------------------------------------------------------

def create_schedule(
    db,
    owner: str,
    source: str,
    preset: str,
    param: int | None = None,
    fire_time: str = "03:00",
    now: datetime | None = None,
    acl: str = "owner",
) -> dict:
    """Create a schedule; upsert on the UNIQUE (owner, source, preset, param,
    fire_time) key.

    Re-adding the same schedule returns the EXISTING row (no duplicate, no
    error) and does NOT reset ``next_fire_at`` (the existing schedule keeps
    its timing). Preset/param are validated fail-fast (ValueError naming the
    problem). ``now`` defaults to the actual now (UTC); ``next_fire_at`` is
    computed with ``expand_next(preset, param, fire_time, now, anchor=now)``
    (creation time is the initial anchor). ``acl`` defaults to ``"owner"``
    (004 MCP create passes through the caller's acl argument; 002 callers
    that omit it get the original ``"owner"`` default).

    Returns the schedule as a dict (see module docstring for keys).
    """
    _validate_preset_param(preset, param)
    ref = _to_aware_utc(now) if now is not None else _now_utc()
    ts = _iso(ref)

    # NULL-safe lookup: SQLite treats NULLs as distinct in the plain
    # UNIQUE(owner, source, preset, param, fire_time) constraint, so a
    # parameterless preset (param NULL) would never collide at the SQL level.
    # Match param explicitly, including the NULL case (IS NULL).
    existing = db.execute(
        "SELECT * FROM schedules "
        "WHERE owner = ? AND source = ? AND preset = ? AND fire_time = ? "
        "  AND (" + ("param IS ?" if param is not None else "param IS NULL") + ")",
        (owner, source, preset, fire_time, param) if param is not None
        else (owner, source, preset, fire_time),
    ).fetchone()
    if existing is not None:
        # Upsert no-op: return the existing row, do NOT reset next_fire_at
        # (the existing schedule keeps its timing).
        return _row_to_dict(existing)

    db.execute(
        "INSERT INTO schedules "
        "(owner, source, preset, param, fire_time, enabled, next_fire_at, "
        " acl, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (owner, source, preset, param, fire_time,
         _next_fire_at_iso(preset, param, fire_time, ref, ref),
         acl, ts, ts),
    )
    return _fetch_row(db, db.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0])


def _next_fire_at_iso(
    preset: str, param, fire_time: str, now: datetime, anchor: datetime
) -> str:
    """expand_next at the naive boundary, re-wrapped to aware-UTC ISO."""
    naive_next = expand_next(preset, param, fire_time,
                             _to_naive_utc(now), _to_naive_utc(anchor))
    return _iso(naive_next)


def list_schedules(db, owner: str | None = None) -> list[dict]:
    """Return schedules, optionally filtered by owner, as dicts.

    Ordered by id ascending (stable, deterministic).
    """
    if owner is None:
        rows = db.execute(
            "SELECT * FROM schedules ORDER BY id ASC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM schedules WHERE owner = ? ORDER BY id ASC",
            (owner,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_schedule(db, schedule_id: int, **fields) -> dict:
    """Update a schedule's fields and return the updated row.

    May change ``preset`` / ``param`` / ``fire_time`` / ``enabled`` /
    ``owner`` / ``source`` / ``acl``. When cadence fields
    (preset/param/fire_time) change, recompute ``next_fire_at`` from the
    ACTUAL now — NOT from the old ``next_fire_at`` (clock-skew guard, R3) —
    with ``anchor`` = the existing ``created_at`` (preserves weekly/monthly
    anchoring). Bumps ``updated_at``.

    Invalid new preset/param -> ValueError (row unchanged).
    """
    current = _fetch_row(db, schedule_id)  # KeyError if the id is missing
    new_preset = fields.get("preset", current["preset"])
    new_param = fields.get("param", current["param"])
    new_fire_time = fields.get("fire_time", current["fire_time"])
    _validate_preset_param(new_preset, new_param)

    new_owner = fields.get("owner", current["owner"])
    new_source = fields.get("source", current["source"])
    new_enabled = fields.get("enabled", current["enabled"])
    new_acl = fields.get("acl", current["acl"])

    cadence_changed = (
        new_preset != current["preset"]
        or new_param != current["param"]
        or new_fire_time != current["fire_time"]
    )

    now = _now_utc()
    ts = _iso(now)
    if cadence_changed:
        # Recompute from actual now, anchored to the existing created_at.
        created_anchor = _parse_iso(current["created_at"])
        next_fire = _next_fire_at_iso(
            new_preset, new_param, new_fire_time, now, created_anchor
        )
    else:
        next_fire = current["next_fire_at"]

    db.execute(
        "UPDATE schedules SET owner = ?, source = ?, preset = ?, param = ?, "
        "fire_time = ?, enabled = ?, next_fire_at = ?, acl = ?, "
        "updated_at = ? WHERE id = ?",
        (new_owner, new_source, new_preset, new_param, new_fire_time,
         new_enabled, next_fire, new_acl, ts, schedule_id),
    )
    return _fetch_row(db, schedule_id)


def delete_schedule(db, schedule_id: int) -> None:
    """Delete the schedule row. No-op if the id does not exist."""
    db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


def due_schedules(db, now: datetime | None = None) -> list[dict]:
    """Schedules that are enabled AND past-due, ordered for firing.

    ``enabled = 1 AND next_fire_at <= now``; oldest ``next_fire_at`` first,
    ties broken by ``id`` ascending (ruling R-11). ``now`` defaults to the
    actual now (UTC).
    """
    ref = _to_aware_utc(now) if now is not None else _now_utc()
    rows = db.execute(
        "SELECT * FROM schedules "
        "WHERE enabled = 1 AND next_fire_at <= ? "
        "ORDER BY next_fire_at ASC, id ASC",
        (_iso(ref),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_and_advance(db, schedule_id: int, fired_at: datetime) -> None:
    """Advance a fired schedule to its next fire time (the durable advance).

    Sets ``next_fire_at = expand_next(preset, param, fire_time, fired_at,
    anchor=created_at)`` and bumps ``updated_at``. ``fired_at`` is the actual
    fire time (run start). This happens AFTER the audit row is written
    (002 invariant 2); one schedule row = one fire's ticket.
    """
    current = _fetch_row(db, schedule_id)
    created_anchor = _parse_iso(current["created_at"])
    next_fire = _next_fire_at_iso(
        current["preset"], current["param"], current["fire_time"],
        fired_at, created_anchor,
    )
    db.execute(
        "UPDATE schedules SET next_fire_at = ?, updated_at = ? WHERE id = ?",
        (next_fire, _iso(_now_utc()), schedule_id),
    )
