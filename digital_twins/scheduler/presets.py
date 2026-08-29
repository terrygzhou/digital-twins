"""Preset cadences: preset_values() + expand_next() pure functions (002).

Contract: specs/002-scheduled-runs/contracts/scheduler.md.
Expansion rules: specs/002-scheduled-runs/data-model.md.

Both functions are pure: the same (preset, param, fire_time, now, anchor)
always yields the same fire time (SC-003). No I/O, no datetime.now()
calls — `now` and `anchor` are parameters.

Expansion rules (data-model.md):
- ``hourly``       → now + 1h. No fire-time anchor.
- ``every-N-hours``→ now + N*3600s. N must be an int ≥ 1.
- ``daily``        → next local fire_time occurrence: today if
                     now < today's fire_time, else tomorrow.
- ``weekly``       → next occurrence on the anchor's weekday
                     (``anchor.weekday()``): this week if now < this
                     week's fire_time on that weekday, else next week.
- ``monthly``      → next occurrence on the anchor's day-of-month
                     (``anchor.day``), clamped to the target month's
                     length (data-model.md ruling R-10): Jan 31 → Feb 28
                     (Feb 29 in a leap year) → Mar 31.

Time handling: naive local wall-clock datetimes only (no tz) — the v1
ceiling noted in plan.md Complexity Tracking. ``fire_time`` is ``HH:MM``.
"""
from __future__ import annotations

import calendar
from datetime import datetime, time as dtime, timedelta

_PRESETS = ("daily", "hourly", "weekly", "monthly", "every-N-hours")


def preset_values() -> list[str]:
    """Return the five preset names, in canonical order (SC-003)."""
    return list(_PRESETS)


def expand_next(
    preset: str,
    param: int | None,
    fire_time: str,
    now: datetime,
    anchor: datetime,
) -> datetime:
    """Compute the next fire time for a schedule.

    Pure function of (preset, param, fire_time, now, anchor) — the same
    inputs always give the same fire time (SC-003 / contracts Invariant 1).

    Args:
        preset: one of the values returned by :func:`preset_values`.
        param: N for ``every-N-hours`` (int ≥ 1); must be ``None`` for
            every other preset.
        fire_time: ``HH:MM`` local time. Ignored by ``hourly`` and
            ``every-N-hours``.
        now: the reference "current" time (the last fire time when this
            is an advance; a scheduler tick's clock when this is a due
            check). Naive local wall-clock.
        anchor: the schedule's creation time (``created_at``). Used only
            by ``weekly`` (weekday) and ``monthly`` (day-of-month).
            Naive local wall-clock.

    Returns:
        The next fire time as a naive local wall-clock datetime, strictly
        after ``now``.

    Raises:
        ValueError: if ``preset`` is unknown; if ``param`` is not
            ``None`` for a preset other than ``every-N-hours``; if
            ``param`` is not an int ≥ 1 for ``every-N-hours``; or if
            ``fire_time`` is not a valid ``HH:MM`` string for the
            time-anchored presets (daily/weekly/monthly).
    """
    if preset not in _PRESETS:
        raise ValueError(f"unknown preset: {preset!r}")

    if preset != "every-N-hours" and param is not None:
        raise ValueError(
            f"param is only valid for 'every-N-hours', not {preset!r}"
        )

    if preset == "hourly":
        return now + timedelta(hours=1)

    if preset == "every-N-hours":
        if not isinstance(param, int) or isinstance(param, bool) or param < 1:
            raise ValueError(
                f"every-N-hours requires param to be an int >= 1, got {param!r}"
            )
        return now + timedelta(hours=param)

    # daily / weekly / monthly: time-anchored presets.
    target = _parse_fire_time(fire_time)
    if target <= now.time():
        delta = 1
    else:
        delta = 0

    if preset == "daily":
        return (now + timedelta(days=delta)).replace(
            hour=target.hour, minute=target.minute, second=0, microsecond=0
        )

    if preset == "weekly":
        return _next_weekly(now, target, anchor.weekday())

    # monthly
    return _next_monthly(now, target, anchor.day)


def _parse_fire_time(fire_time: str) -> dtime:
    """Parse ``HH:MM`` into a :class:`datetime.time` (zero seconds)."""
    try:
        hh, mm = fire_time.split(":")
        hour, minute = int(hh), int(mm)
    except (AttributeError, ValueError):
        raise ValueError(f"fire_time must be 'HH:MM', got {fire_time!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"fire_time out of range: {fire_time!r}")
    return dtime(hour, minute)


def _next_weekly(now: datetime, target: dtime, weekday: int) -> datetime:
    """Next occurrence of ``target`` on a given weekday, strictly after now."""
    day = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
    candidate = datetime.combine(day, target)
    if candidate <= now:
        candidate += timedelta(weeks=1)
    return candidate


def _next_monthly(now: datetime, target: dtime, day: int) -> datetime:
    """Next occurrence of day ``day`` in a future month, clamped to month
    length (data-model.md ruling R-10: Jan 31 → Feb 28/29 → Mar 31)."""
    last_day = calendar.monthrange(now.year, now.month)[1]
    clamped_day = min(day, last_day)
    candidate = now.replace(
        day=clamped_day,
        hour=target.hour,
        minute=target.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now:
        # Roll to next month.
        if now.month == 12:
            nxt = now.replace(year=now.year + 1, month=1, day=1)
        else:
            nxt = now.replace(month=now.month + 1, day=1)
        clamped = min(day, calendar.monthrange(nxt.year, nxt.month)[1])
        candidate = nxt.replace(
            day=clamped,
            hour=target.hour,
            minute=target.minute,
            second=0,
            microsecond=0,
        )
    return candidate
