from datetime import datetime

import pytest

from growing_degree_days import daily_gdd, estimate_harvest_date_gdd


def test_daily_gdd_basic():
    # avg(30,20)=25, base 10 -> 15 heat units
    assert daily_gdd(30, 20, base_temp_c=10) == 15


def test_daily_gdd_below_base_is_zero_not_negative():
    # avg(8,4)=6, base 10 -> would be negative; must clamp to 0
    assert daily_gdd(8, 4, base_temp_c=10) == 0


def test_daily_gdd_respects_cap():
    # Without cap: avg(40,30)=35 - 10 = 25
    assert daily_gdd(40, 30, base_temp_c=10) == 25
    # With cap at 32: tmax clamped to 32, tmin(30) untouched -> avg(32,30)=31-10=21
    assert daily_gdd(40, 30, base_temp_c=10, cap_c=32) == 21


def test_estimate_harvest_date_unknown_class_returns_none():
    result = estimate_harvest_date_gdd(
        "unknown_class", {"green": 200}, base_temp_c=10, assumed_avg_temp_c=27,
    )
    assert result is None


def test_estimate_harvest_date_zero_requirement_is_today():
    from_date = datetime(2026, 6, 1)
    result = estimate_harvest_date_gdd(
        "fully_ripened", {"fully_ripened": 0}, base_temp_c=10,
        from_date=from_date, assumed_avg_temp_c=27,
    )
    assert result == from_date


def test_estimate_harvest_date_uses_flat_assumption_without_coordinates():
    """No lat/lon given -> must use assumed_avg_temp_c every day, not crash
    or silently invent a different number."""
    from_date = datetime(2026, 6, 1)
    base = 10
    assumed = 27.0
    daily = daily_gdd(assumed, assumed, base)  # 17/day
    required = daily * 3  # threshold reached on the 3rd day (day_offset 0,1,2)
    result = estimate_harvest_date_gdd(
        "green", {"green": required}, base_temp_c=base,
        from_date=from_date, assumed_avg_temp_c=assumed,
    )
    assert result is not None
    assert (result - from_date).days == 2


def test_estimate_harvest_date_gives_up_after_lookahead_window():
    """If the requirement can never be reached in the lookahead window (e.g.
    assumed temp below base so daily GDD is 0), it must return the edge of
    the window rather than loop forever or fabricate an earlier date."""
    from_date = datetime(2026, 6, 1)
    result = estimate_harvest_date_gdd(
        "green", {"green": 999999}, base_temp_c=10,
        from_date=from_date, assumed_avg_temp_c=27.0, max_lookahead_days=10,
    )
    assert (result - from_date).days == 10
