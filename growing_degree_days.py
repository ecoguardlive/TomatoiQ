"""
Growing Degree Day (GDD) harvest estimation
--------------------------------------------
Replaces the flat "green=14 days, half_ripened=6 days" lookup table with an
estimate grounded in actual/forecast temperature at your farm's location.

Why GDD instead of a fixed day count:
Tomato ripening rate depends heavily on temperature, not on the calendar.
A tomato sitting in a cool week ripens slower than one in a hot week. GDD
accumulates "heat units" per day above a base temperature the plant needs
before ripening progresses, and estimates harvest once a class-specific GDD
threshold is reached. This is the same approach agronomists use for crop
maturity models (e.g. corn, tomato) -- see e.g. UC Davis / Cornell extension
GDD guides. The exact GDD-to-ripeness thresholds below are configurable
defaults based on typical published tomato heat-unit requirements; tune them
for your variety once you have a season of ground-truth data.

Data source: Open-Meteo (https://open-meteo.com), a free weather API that
needs no API key. We use its 16-day daily forecast for future days, and its
archive endpoint for recent historical days when a forecast isn't available
(e.g. if the run happens after the forecast window, or offline testing).

If no location is configured, or the network call fails, this module falls
back to a fixed "assumed average daily temperature" so the system still
works (degrades gracefully to a GDD estimate with no live weather rather
than crashing or reverting silently to different behavior).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def daily_gdd(tmax_c: float, tmin_c: float, base_temp_c: float, cap_c: Optional[float] = None) -> float:
    """Single-day GDD using the standard averaging method:
        GDD = max(0, (tmax + tmin) / 2 - base_temp)
    An optional upper cap (some crops stop accumulating heat benefit above a
    ceiling temperature) truncates tmax/tmin before averaging if provided.
    """
    if cap_c is not None:
        tmax_c = min(tmax_c, cap_c)
        tmin_c = min(tmin_c, cap_c)
    avg = (tmax_c + tmin_c) / 2.0
    return max(0.0, avg - base_temp_c)


def fetch_daily_temps(lat: float, lon: float, start_date: datetime, num_days: int,
                       timeout: float = 5.0) -> list[dict]:
    """Returns a list of {date, tmax_c, tmin_c} for num_days starting at
    start_date, using forecast data where available and falling back to the
    historical archive (same calendar window, prior year) for any day too
    far in the future for the forecast API to cover.

    Raises RuntimeError if the network call fails or `requests` isn't
    installed -- callers should catch this and fall back to
    `assumed_avg_temp_c`.
    """
    if requests is None:
        raise RuntimeError("The 'requests' package is required for live weather lookups.")

    end_date = start_date + timedelta(days=num_days - 1)
    results: list[dict] = []

    # Open-Meteo's free forecast endpoint covers ~16 days ahead.
    forecast_horizon = start_date + timedelta(days=15)
    forecast_end = min(end_date, forecast_horizon)

    if forecast_end >= start_date:
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": forecast_end.strftime("%Y-%m-%d"),
        }
        resp = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()["daily"]
        for d, tmax, tmin in zip(data["time"], data["temperature_2m_max"], data["temperature_2m_min"]):
            results.append({"date": d, "tmax_c": tmax, "tmin_c": tmin})

    # For any remaining days beyond the forecast horizon, use last year's
    # observed temps for the same calendar dates as a seasonal proxy.
    remaining_start = forecast_end + timedelta(days=1)
    if remaining_start <= end_date:
        proxy_start = remaining_start.replace(year=remaining_start.year - 1)
        proxy_end = end_date.replace(year=end_date.year - 1)
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
            "start_date": proxy_start.strftime("%Y-%m-%d"),
            "end_date": proxy_end.strftime("%Y-%m-%d"),
        }
        resp = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()["daily"]
        current = remaining_start
        for tmax, tmin in zip(data["temperature_2m_max"], data["temperature_2m_min"]):
            results.append({"date": current.strftime("%Y-%m-%d"), "tmax_c": tmax, "tmin_c": tmin})
            current += timedelta(days=1)

    return results


def estimate_harvest_date_gdd(
    ripeness_class: str,
    gdd_required_map: dict,
    base_temp_c: float,
    from_date: Optional[datetime] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    assumed_avg_temp_c: float = 27.0,
    max_lookahead_days: int = 45,
    cap_c: Optional[float] = None,
) -> Optional[datetime]:
    """Walks forward day by day accumulating GDD until the class's required
    heat-unit total is reached, and returns that date.

    - If `lat`/`lon` are given, pulls real forecast/historical temps.
    - If not given, or the network call fails, uses a flat
      `assumed_avg_temp_c` for every day (still GDD-based, just without live
      weather -- this keeps behavior predictable instead of silently
      changing methodology).
    - Returns None if the class isn't in gdd_required_map (e.g. an unknown
      class name), matching the old flat-table function's contract.
    """
    required = gdd_required_map.get(ripeness_class)
    if required is None:
        return None
    if required <= 0:
        return from_date or datetime.now()

    from_date = from_date or datetime.now()
    start = datetime(from_date.year, from_date.month, from_date.day)

    daily_temps = None
    if lat is not None and lon is not None:
        try:
            daily_temps = fetch_daily_temps(lat, lon, start, max_lookahead_days)
        except Exception:
            daily_temps = None  # fall through to the flat assumption below

    accumulated = 0.0
    for day_offset in range(max_lookahead_days):
        if daily_temps is not None and day_offset < len(daily_temps):
            day = daily_temps[day_offset]
            gdd_today = daily_gdd(day["tmax_c"], day["tmin_c"], base_temp_c, cap_c)
        else:
            # Flat fallback: treat the assumed average as both tmax and tmin.
            gdd_today = daily_gdd(assumed_avg_temp_c, assumed_avg_temp_c, base_temp_c, cap_c)

        accumulated += gdd_today
        if accumulated >= required:
            return start + timedelta(days=day_offset)

    # Didn't reach the threshold within the lookahead window.
    return start + timedelta(days=max_lookahead_days)
