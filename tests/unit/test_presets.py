"""Property-style tests for scheduler.presets (T003, SC-003).

Fixed known-answer inputs — no property framework; the 001 suite is
plain pytest. Each case pins (preset, param, fire_time, now, anchor) →
the exact fire time expand_next must return.

Pure-function contract (002 contracts/scheduler.md):
- expand_next is a pure function of (preset, param, fire_time, now, anchor)
- param validation: int >= 1, only for every-N-hours; else ValueError
- preset_values() returns exactly the five preset names
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from digital_twins.scheduler import presets


# --- preset_values --------------------------------------------------------


def test_preset_values_returns_exactly_five():
    values = presets.preset_values()
    assert values == [
        "daily",
        "hourly",
        "weekly",
        "monthly",
        "every-N-hours",
    ]
    # order is part of the contract (SC-003 determinism)
    assert values == presets.preset_values()


# --- daily ----------------------------------------------------------------


def test_daily_fires_today_when_now_before_fire_time():
    # now = 02:00 today, fire_time 03:00 → today 03:00
    now = datetime(2025, 6, 15, 2, 0, 0)
    anchor = datetime(2025, 6, 1, 12, 0, 0)
    out = presets.expand_next("daily", None, "03:00", now, anchor)
    assert out == datetime(2025, 6, 15, 3, 0, 0)


def test_daily_fires_tomorrow_when_now_after_fire_time():
    # now = 04:00 today, fire_time 03:00 → tomorrow 03:00
    now = datetime(2025, 6, 15, 4, 0, 0)
    anchor = datetime(2025, 6, 1, 12, 0, 0)
    out = presets.expand_next("daily", None, "03:00", now, anchor)
    assert out == datetime(2025, 6, 16, 3, 0, 0)


def test_daily_ignores_anchor():
    # daily does not use anchor; different anchors give same result
    now = datetime(2025, 6, 15, 4, 0, 0)
    a1 = datetime(2025, 1, 1, 0, 0, 0)
    a2 = datetime(2024, 12, 25, 0, 0, 0)
    assert presets.expand_next("daily", None, "03:00", now, a1) == \
        presets.expand_next("daily", None, "03:00", now, a2)


# --- hourly ----------------------------------------------------------------


def test_hourly_is_now_plus_one_hour():
    now = datetime(2025, 6, 15, 14, 30, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    out = presets.expand_next("hourly", None, "03:00", now, anchor)
    assert out == now + timedelta(hours=1)


def test_hourly_ignores_fire_time_and_anchor():
    now = datetime(2025, 6, 15, 14, 30, 0)
    a = datetime(2025, 1, 1, 0, 0, 0)
    assert presets.expand_next("hourly", None, "23:59", now, a) == \
        presets.expand_next("hourly", None, "00:00", now, a)
    assert presets.expand_next("hourly", None, "03:00", now, a) == \
        presets.expand_next("hourly", None, "03:00", now, a + timedelta(days=90))


# --- every-N-hours ---------------------------------------------------------


def test_every_n_hours_adds_n_hours():
    now = datetime(2025, 6, 15, 14, 0, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    out = presets.expand_next("every-N-hours", 2, "03:00", now, anchor)
    assert out == now + timedelta(hours=2)


def test_every_n_hours_with_large_n():
    now = datetime(2025, 6, 15, 23, 0, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    out = presets.expand_next("every-N-hours", 25, "03:00", now, anchor)
    # 23:00 + 25h = next day 24:00 = next day 00:00 (pure datetime arithmetic)
    assert out == now + timedelta(hours=25)


def test_every_n_hours_ignores_fire_time():
    now = datetime(2025, 6, 15, 14, 0, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    assert presets.expand_next("every-N-hours", 3, "00:00", now, anchor) == \
        presets.expand_next("every-N-hours", 3, "23:59", now, anchor)


# --- weekly -----------------------------------------------------------------


def _friday_anchor() -> datetime:
    # 2025-06-20 is a Friday
    return datetime(2025, 6, 20, 9, 0, 0)


def test_weekly_fires_this_week_when_now_before_fire_time():
    # anchor Friday 2025-06-20, now Thu 2025-06-19 04:00 → this week's Fri 03:00
    now = datetime(2025, 6, 19, 4, 0, 0)
    out = presets.expand_next("weekly", None, "03:00", now, _friday_anchor())
    assert out == datetime(2025, 6, 20, 3, 0, 0)


def test_weekly_fires_next_week_when_now_after_fire_time():
    # now Fri 2025-06-20 04:00 (after this week's 03:00) → next Fri 03:00
    now = datetime(2025, 6, 20, 4, 0, 0)
    out = presets.expand_next("weekly", None, "03:00", now, _friday_anchor())
    assert out == datetime(2025, 6, 27, 3, 0, 0)


def test_weekly_uses_anchor_weekday_not_now_weekday():
    # anchor is a Monday (2025-06-16); now is mid-week Wed 2025-06-18 04:00
    # → next Monday 03:00 = 2025-06-23
    now = datetime(2025, 6, 18, 4, 0, 0)
    anchor = datetime(2025, 6, 16, 12, 0, 0)  # Monday
    out = presets.expand_next("weekly", None, "03:00", now, anchor)
    assert out == datetime(2025, 6, 23, 3, 0, 0)


# --- monthly -----------------------------------------------------------------


def test_monthly_clamps_day_to_shorter_month():
    # anchor day=31 (Jan 31 2025). now = Mar 31 2025 04:00 (after Mar 31 03:00)
    # → April 30 03:00 (April has 30 days; 31 clamps to 30)
    now = datetime(2025, 3, 31, 4, 0, 0)
    anchor = datetime(2025, 1, 31, 12, 0, 0)
    out = presets.expand_next("monthly", None, "03:00", now, anchor)
    assert out == datetime(2025, 4, 30, 3, 0, 0)


def test_monthly_known_answer_jan31_to_feb28_to_mar31():
    # data-model.md ruling R-10: Jan 31 → Feb 28 (28/29 in leap) → Mar 31
    # Step 1: now = Jan 31 04:00 (after Jan 31 03:00) → Feb 28 03:00
    anchor = datetime(2025, 1, 31, 12, 0, 0)
    now = datetime(2025, 1, 31, 4, 0, 0)
    feb = presets.expand_next("monthly", None, "03:00", now, anchor)
    assert feb == datetime(2025, 2, 28, 3, 0, 0)
    # Step 2: now = Feb 28 04:00 → Mar 31 03:00 (anchor day 31 clamps to 31)
    now = datetime(2025, 2, 28, 4, 0, 0)
    mar = presets.expand_next("monthly", None, "03:00", now, anchor)
    assert mar == datetime(2025, 3, 31, 3, 0, 0)


def test_monthly_leap_year_feb29():
    # 2024 is a leap year. anchor day=31, now = Jan 31 2024 04:00 → Feb 29 03:00
    anchor = datetime(2024, 1, 31, 12, 0, 0)
    now = datetime(2024, 1, 31, 4, 0, 0)
    out = presets.expand_next("monthly", None, "03:00", now, anchor)
    assert out == datetime(2024, 2, 29, 3, 0, 0)


def test_monthly_fires_this_month_when_now_before_fire_time():
    # anchor day=15, now = Jun 10 04:00 → Jun 15 03:00
    anchor = datetime(2025, 1, 15, 12, 0, 0)
    now = datetime(2025, 6, 10, 4, 0, 0)
    out = presets.expand_next("monthly", None, "03:00", now, anchor)
    assert out == datetime(2025, 6, 15, 3, 0, 0)


def test_monthly_fires_next_month_when_now_after_fire_time():
    # anchor day=15, now = Jun 15 04:00 → Jul 15 03:00
    anchor = datetime(2025, 1, 15, 12, 0, 0)
    now = datetime(2025, 6, 15, 4, 0, 0)
    out = presets.expand_next("monthly", None, "03:00", now, anchor)
    assert out == datetime(2025, 7, 15, 3, 0, 0)


# --- param validation ---------------------------------------------------------


def test_param_rejected_for_non_every_n_presets():
    for preset in ("daily", "hourly", "weekly", "monthly"):
        now = datetime(2025, 6, 15, 4, 0, 0)
        anchor = datetime(2025, 1, 1, 0, 0, 0)
        with pytest.raises(ValueError):
            presets.expand_next(preset, 5, "03:00", now, anchor)


def test_every_n_hours_param_must_be_positive_int():
    now = datetime(2025, 6, 15, 4, 0, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            presets.expand_next("every-N-hours", bad, "03:00", now, anchor)


def test_every_n_hours_param_bool_rejected():
    # bool is a subclass of int; True would pass `int >= 1` — reject explicitly
    now = datetime(2025, 6, 15, 4, 0, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError):
        presets.expand_next("every-N-hours", True, "03:00", now, anchor)


def test_unknown_preset_raises_valueerror():
    now = datetime(2025, 6, 15, 4, 0, 0)
    anchor = datetime(2025, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError):
        presets.expand_next("fortnightly", None, "03:00", now, anchor)
    with pytest.raises(ValueError):
        presets.expand_next("", None, "03:00", now, anchor)


# --- determinism (SC-003) -------------------------------------------------------


def test_expand_next_is_deterministic_ten_calls():
    now = datetime(2025, 6, 15, 4, 0, 0)
    anchor = datetime(2025, 1, 31, 12, 0, 0)
    results = [
        presets.expand_next("monthly", None, "03:00", now, anchor)
        for _ in range(10)
    ]
    assert all(r == results[0] for r in results)
    # same inputs across preset variants
    for preset, param in (
        ("daily", None),
        ("hourly", None),
        ("weekly", None),
        ("every-N-hours", 4),
    ):
        base = presets.expand_next(preset, param, "03:00", now, anchor)
        again = [
            presets.expand_next(preset, param, "03:00", now, anchor)
            for _ in range(10)
        ]
        assert all(r == base for r in again)
