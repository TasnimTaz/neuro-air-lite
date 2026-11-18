"""Deterministic, read-only reference for the Tai Po fire case study.

This module is deliberately small and boring: fixed windows, fixed SQL, fixed
expected values, no model, no writes. It mirrors the Algorithm 1 sanity checks
in ``scripts/check_data.py`` so the demo and the daemon share one source of
truth.

Two cautions are kept in the output rather than hidden:

* the 54.6 ug/m3 value is an hourly peak inside the 24-hour post-fire window,
  so comparing it with a 24-hour AQO is a screening comparison, not a legal
  compliance determination;
* the OSM building counts are proximity-screening counts from the ingested 2026
  snapshot, not a reproduction of the paper's 1,090 total / 293 residential
  numbers.

The reference uses ``make_ro_engine()`` and also issues ``SET TRANSACTION READ
ONLY`` on its connection, so passing a more privileged engine still cannot turn
a reference read into a write.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.env import load_agent_env
from domains.hk.db import make_ro_engine

STATION_CODE = "taipo"
FIRE_SITE_LAT = 22.4468
FIRE_SITE_LON = 114.1754

# Algorithm 1 windows used by scripts/check_data.py.
PEAK_HOUR = "2025-11-26 20:00"
PRE_START = "2025-11-23 14:00"
PRE_END = "2025-11-26 15:00"
POST_START = "2025-11-26 15:00"
POST_END = "2025-11-27 15:00"

# Expert reanalysis in the paper (main text and Supplementary Note 3):
# * matched-hour baseline: same clock hour over the 14 preceding days
#   (12-25 November 2025);
# * network contrast: event evening 18:00-23:00 on 26 November, differenced
#   against the same clock-hours over those 14 days;
# * station-specific excess: the nearest station's rise minus the mean rise of
#   the other 17 stations.
BASELINE_START = "2025-11-12 00:00"
BASELINE_END = "2025-11-26 00:00"
EVENT_EVENING_START = "2025-11-26 18:00"
EVENT_EVENING_END = "2025-11-27 00:00"
EVENT_EVENING_HOURS = (18, 19, 20, 21, 22, 23)

# Current Hong Kong AQO (effective 2025-04-11) and WHO 2021 values. Only values
# used by the reference output are repeated here; the KB remains the source for
# the full table.
HK_AQO_PM25_24H = 37.5
HK_AQO_NO2_1H = 200.0
WHO_PM25_24H = 15.0
WHO_NO2_24H = 25.0


@dataclass(frozen=True)
class Spec:
    """One expected value and the tolerance used to call it a match."""

    key: str
    label: str
    expected: Any
    tolerance: float | None = None
    unit: str = ""
    source: str = ""
    note: str = ""


@dataclass(frozen=True)
class Check:
    """A single observed-versus-expected comparison."""

    key: str
    label: str
    observed: Any
    expected: Any
    tolerance: float | None
    ok: bool
    unit: str = ""
    source: str = ""
    note: str = ""


# The first seven values are the paper/ingest sanity values. The remaining
# values are deterministic counts from this database snapshot; they are kept so
# the daemon notices accidental re-ingests or a changed OSM/sensitive-area
# snapshot. They are not claims of paper reproduction.
SPECS: tuple[Spec, ...] = (
    Spec(
        "no2_at_2000",
        "NO2 at 2025-11-26 20:00",
        123.2,
        0.05,
        "ug/m3",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "aqhi_at_2000",
        "AQHI at 2025-11-26 20:00",
        5,
        0,
        "",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "pm25_post_peak",
        "PM2.5 peak, 24 h post-fire window",
        54.6,
        0.05,
        "ug/m3",
        "paper sanity / scripts/check_data.py",
        "Hourly peak; compare with a 24 h AQO only as a screening check.",
    ),
    Spec(
        "pm25_pre_mean",
        "PM2.5 pre-fire mean",
        26.68,
        0.01,
        "ug/m3",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "no2_post_peak",
        "NO2 peak, 24 h post-fire window",
        123.2,
        0.05,
        "ug/m3",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "no2_pre_mean",
        "NO2 pre-fire mean",
        48.90,
        0.01,
        "ug/m3",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "wind_speed_2000",
        "Wind speed at 2025-11-26 20:00",
        0.389,
        0.001,
        "m/s",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "wind_degree_2000",
        "Wind bearing at 2025-11-26 20:00",
        207.0,
        0.5,
        "degrees",
        "paper sanity / scripts/check_data.py",
        "Database value is 206.565 degrees; the paper rounds to 207.",
    ),
    Spec(
        "wind_direction_2000",
        "Wind compass direction at 2025-11-26 20:00",
        "SW",
        None,
        "",
        "paper sanity / scripts/check_data.py",
    ),
    Spec(
        "sensitive_education_fire_1500",
        "Educational sensitive areas within 1.5 km of the fire site",
        45,
        0,
        "count",
        "sensitive_area / 2026 snapshot",
        "Fire-site-centred count; the station-centred check_agent truth is 53.",
    ),
    Spec(
        "sensitive_health_fire_1500",
        "Health-care sensitive areas within 1.5 km of the fire site",
        1,
        0,
        "count",
        "sensitive_area / 2026 snapshot",
    ),
    Spec(
        "sensitive_worship_fire_1500",
        "Places of worship within 1.5 km of the fire site",
        50,
        0,
        "count",
        "sensitive_area / 2026 snapshot",
    ),
    Spec(
        "sensitive_education_station_1500",
        "Educational sensitive areas within 1.5 km of the Tai Po station",
        53,
        0,
        "count",
        "check_agent #8",
        "Station-centred count, not the fire-site count.",
    ),
    Spec(
        "buildings_2km",
        "OSM buildings within 2 km of the fire site",
        4434,
        0,
        "count",
        "osm_feature / 2026 snapshot",
        "Proximity screening count; not the paper's 1,090 buildings.",
    ),
    Spec(
        "residential_1500",
        "Residential OSM buildings within 1.5 km of the fire site",
        1153,
        0,
        "count",
        "osm_feature / 2026 snapshot",
        "Proximity screening count; not the paper's 293 residential buildings.",
    ),
    Spec(
        "buildings_25km",
        "OSM buildings within 2.5 km of the fire site",
        5661,
        0,
        "count",
        "osm_feature / 2026 snapshot",
        "The ingest AOI radius; proximity screening only.",
    ),
    # Expert-reanalysis values from the paper's Tai Po section and Supplementary
    # Note 3. These are not system-output sanity values; they are the paper's
    # own matched-hour and network-contrast checks.
    Spec(
        "matched_hour_z",
        "Tai Po NO2 at 20:00 vs its 14-day matched-hour baseline (z)",
        1.9,
        0.05,
        "z",
        "paper main text / Supplementary Note 3",
        "Sample standard deviation (ddof=1), same 20:00 hour on 12-25 Nov 2025.",
    ),
    Spec(
        "matched_hour_max_prev_14d",
        "Maximum Tai Po NO2 at 20:00 over the preceding 14 days",
        125.5,
        0.05,
        "ug/m3",
        "paper main text / Supplementary Note 3",
        "A non-fire evening reached this value; the fire-evening peak was 123.2.",
    ),
    Spec(
        "network_mean_rise",
        "Territory-wide NO2 rise over the event evening, other 17 stations",
        19.6,
        0.05,
        "ug/m3",
        "paper main text / Supplementary Note 3",
        "Event evening 18:00-23:00 minus the same clock-hours on 12-25 Nov.",
    ),
    Spec(
        "nearest_station_rise_vs_network",
        "Tai Po PM2.5 rise above the other-17-station mean rise",
        7.4,
        0.05,
        "ug/m3",
        "paper main text / Supplementary Note 3",
        "Paper reports the nearest station as first of 18 for PM2.5 elevation.",
    ),
    Spec(
        "pm25_rank_of_nearest",
        "Tai Po PM2.5 evening-elevation rank among the 18 stations",
        1,
        0,
        "rank",
        "paper main text / Supplementary Note 3",
        "Rank 1 of 18; larger rises rank first.",
    ),
)

EXPECTED = {spec.key: spec.expected for spec in SPECS}


METRICS_SQL = text(
    """
    SELECT
      max(pm2_5) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS pm25_post_peak,
      avg(pm2_5) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS pm25_post_mean,
      avg(pm2_5) FILTER (WHERE datetime >= :pre_start AND datetime < :pre_end)
        AS pm25_pre_mean,
      count(pm2_5) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS pm25_post_n,
      max(no2) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS no2_post_peak,
      avg(no2) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS no2_post_mean,
      avg(no2) FILTER (WHERE datetime >= :pre_start AND datetime < :pre_end)
        AS no2_pre_mean,
      count(no2) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS no2_post_n,
      max(aqhi) FILTER (WHERE datetime >= :post_start AND datetime < :post_end)
        AS aqhi_post_peak,
      max(pm2_5) FILTER (WHERE datetime = CAST(:peak_hour AS timestamp))
        AS pm25_at_2000,
      max(no2) FILTER (WHERE datetime = CAST(:peak_hour AS timestamp))
        AS no2_at_2000,
      max(aqhi) FILTER (WHERE datetime = CAST(:peak_hour AS timestamp))
        AS aqhi_at_2000
    FROM hourly_station_air_quality a
    JOIN air_quality_station s ON s.id = a.station_id
    WHERE s.station_code = :station_code
      AND datetime >= :pre_start
      AND datetime < :post_end
    """
)

MATCHED_HOUR_SQL = text(
    """
    SELECT a.datetime, a.no2
    FROM hourly_station_air_quality a
    JOIN air_quality_station s ON s.id = a.station_id
    WHERE s.station_code = :station_code
      AND a.datetime >= CAST(:baseline_start AS timestamp)
      AND a.datetime < CAST(:baseline_end AS timestamp)
      AND extract(hour from a.datetime) = :matched_hour
      AND a.no2 IS NOT NULL
    ORDER BY a.datetime
    """
)

EVENING_NETWORK_SQL = text(
    """
    WITH event AS (
      SELECT
        s.station_code,
        avg(a.no2) AS no2_event,
        avg(a.pm2_5) AS pm25_event,
        avg(a.pm10) AS pm10_event
      FROM hourly_station_air_quality a
      JOIN air_quality_station s ON s.id = a.station_id
      WHERE a.datetime >= CAST(:event_start AS timestamp)
        AND a.datetime < CAST(:event_end AS timestamp)
      GROUP BY s.station_code
    ), baseline AS (
      SELECT
        s.station_code,
        avg(a.no2) AS no2_base,
        avg(a.pm2_5) AS pm25_base,
        avg(a.pm10) AS pm10_base
      FROM hourly_station_air_quality a
      JOIN air_quality_station s ON s.id = a.station_id
      WHERE a.datetime >= CAST(:baseline_start AS timestamp)
        AND a.datetime < CAST(:baseline_end AS timestamp)
        AND extract(hour from a.datetime) BETWEEN 18 AND 23
      GROUP BY s.station_code
    )
    SELECT
      e.station_code,
      e.no2_event, b.no2_base, e.no2_event - b.no2_base AS no2_rise,
      e.pm25_event, b.pm25_base, e.pm25_event - b.pm25_base AS pm25_rise,
      e.pm10_event, b.pm10_base, e.pm10_event - b.pm10_base AS pm10_rise
    FROM event e
    JOIN baseline b ON b.station_code = e.station_code
    WHERE e.no2_event IS NOT NULL
      AND b.no2_base IS NOT NULL
    ORDER BY e.station_code
    """
)

WIND_SQL = text(
    """
    SELECT
      w.wind_speed,
      w.wind_gust,
      w.wind_degree,
      w.wind_direction::text AS wind_direction,
      w.temperature,
      w.humidity,
      w.pressure
    FROM hourly_station_weather w
    JOIN weather_station s ON s.id = w.station_id
    WHERE s.station_code = 'wx_' || :station_code
      AND w.datetime = CAST(:peak_hour AS timestamp)
    """
)

STATION_SQL = text(
    """
    SELECT
      ST_Y(coordinate::geometry) AS lat,
      ST_X(coordinate::geometry) AS lon
    FROM air_quality_station
    WHERE station_code = :station_code
    """
)

SENSITIVE_COUNTS_SQL = text(
    """
    SELECT sa.category::text AS category, count(*) AS n
    FROM sensitive_area sa
    WHERE ST_DWithin(
      sa.geometry::geography,
      ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
      :radius_m
    )
    GROUP BY sa.category
    """
)

NEAREST_SENSITIVE_SQL = text(
    """
    SELECT
      COALESCE(sa.name, '(unnamed)') AS name,
      sa.category::text AS category,
      ST_Distance(
        sa.geometry::geography,
        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
      ) AS distance_m
    FROM sensitive_area sa
    WHERE ST_DWithin(
      sa.geometry::geography,
      ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
      :radius_m
    )
    ORDER BY distance_m ASC
    LIMIT :limit
    """
)

BUILDINGS_SQL = text(
    """
    SELECT
      count(*) FILTER (
        WHERE ST_DWithin(
          geometry::geography,
          ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
          2000
        )
      ) AS buildings_2km,
      count(*) FILTER (
        WHERE sub_type IN ('residential', 'apartments', 'house', 'terrace', 'dormitory')
          AND ST_DWithin(
            geometry::geography,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
            1500
          )
      ) AS residential_1500,
      count(*) FILTER (
        WHERE ST_DWithin(
          geometry::geography,
          ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
          2500
        )
      ) AS buildings_25km
    FROM osm_feature
    WHERE main_type = 'BUILDING'
    """
)

SQL_STATEMENTS = (
    METRICS_SQL,
    MATCHED_HOUR_SQL,
    EVENING_NETWORK_SQL,
    WIND_SQL,
    STATION_SQL,
    SENSITIVE_COUNTS_SQL,
    NEAREST_SENSITIVE_SQL,
    BUILDINGS_SQL,
)


def _category_count(rows: list[Mapping[str, Any]], category: str) -> int:
    for row in rows:
        if row["category"] == category:
            return int(row["n"])
    return 0


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _matches(observed: Any, expected: Any, tolerance: float | None) -> bool:
    if expected is None:
        return observed is None
    if isinstance(expected, str):
        return observed == expected
    if observed is None:
        return False
    tolerance = 0.0 if tolerance is None else tolerance
    try:
        return math.isclose(
            float(observed), float(expected), rel_tol=0.0, abs_tol=tolerance
        )
    except (TypeError, ValueError):
        return False


def evaluate_reference(observed: Mapping[str, Any]) -> list[Check]:
    """Compare observed values with the fixed reference snapshot."""
    checks: list[Check] = []
    for spec in SPECS:
        value = observed.get(spec.key)
        checks.append(
            Check(
                key=spec.key,
                label=spec.label,
                observed=value,
                expected=spec.expected,
                tolerance=spec.tolerance,
                ok=_matches(value, spec.expected, spec.tolerance),
                unit=spec.unit,
                source=spec.source,
                note=spec.note,
            )
        )
    return checks


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _build_anomaly_assessment(
    observed: Mapping[str, Any],
    matched_hour_rows: list[Mapping[str, Any]],
    network_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply the paper's fixed matched-hour and evening network definitions.

    The rule below is deliberately simple and explicit; it is not an LLM
    judgement. It encodes what the paper's reanalysis says a single-source
    plume would need to show. The values are reported even when the rule is
    indeterminate, so the reader can inspect the evidence.
    """
    matched_values = [
        float(row["no2"]) for row in matched_hour_rows if row.get("no2") is not None
    ]
    matched_mean = _mean(matched_values)
    matched_sd = (
        statistics.stdev(matched_values) if len(matched_values) > 1 else None
    )
    matched_max = max(matched_values) if matched_values else None
    no2_peak = observed.get("no2_at_2000")
    z = None
    if (
        no2_peak is not None
        and matched_mean is not None
        and matched_sd not in (None, 0)
    ):
        z = (float(no2_peak) - matched_mean) / matched_sd

    tai_po = next(
        (row for row in network_rows if row["station_code"] == STATION_CODE),
        None,
    )
    others = [
        row for row in network_rows if row["station_code"] != STATION_CODE
    ]
    other_no2_rises = [
        float(row["no2_rise"]) for row in others if row.get("no2_rise") is not None
    ]
    other_pm25_rises = [
        float(row["pm25_rise"]) for row in others if row.get("pm25_rise") is not None
    ]
    network_no2_rise = _mean(other_no2_rises)
    network_pm25_rise = _mean(other_pm25_rises)
    tai_no2_rise = (
        float(tai_po["no2_rise"])
        if tai_po is not None and tai_po.get("no2_rise") is not None
        else None
    )
    tai_pm25_rise = (
        float(tai_po["pm25_rise"])
        if tai_po is not None and tai_po.get("pm25_rise") is not None
        else None
    )
    pm25_rise_values = [
        float(row["pm25_rise"])
        for row in network_rows
        if row.get("pm25_rise") is not None
    ]
    pm25_rank = None
    if tai_pm25_rise is not None and pm25_rise_values:
        pm25_rank = 1 + sum(value > tai_pm25_rise for value in pm25_rise_values)
    pm25_excess = (
        tai_pm25_rise - network_pm25_rise
        if tai_pm25_rise is not None and network_pm25_rise is not None
        else None
    )

    no2_pre_mean = observed.get("no2_pre_mean")
    no2_post_peak = observed.get("no2_post_peak")
    raw_ratio = (
        float(no2_post_peak) / float(no2_pre_mean)
        if no2_post_peak is not None and no2_pre_mean not in (None, 0)
        else None
    )
    pm25_pre_mean = observed.get("pm25_pre_mean")
    pm25_post_peak = observed.get("pm25_post_peak")
    pm25_raw_ratio = (
        float(pm25_post_peak) / float(pm25_pre_mean)
        if pm25_post_peak is not None and pm25_pre_mean not in (None, 0)
        else None
    )

    # Fixed, pre-declared decision rule. A single-source plume needs an unusual
    # nearest-station excursion, a new matched-hour maximum, a modest network
    # rise, and a local PM2.5 excess. If the matched-hour evidence instead shows
    # an ordinary high value and the network rises with it, the evidence points
    # away from a single-source plume.
    if (
        z is None
        or matched_max is None
        or no2_peak is None
        or network_no2_rise is None
        or pm25_excess is None
    ):
        supports_local_plume = "indeterminate"
    elif (
        z >= 2.0
        and float(no2_peak) > matched_max
        and network_no2_rise < 5.0
        and pm25_excess >= 5.0
    ):
        supports_local_plume = "true"
    elif (
        (z < 2.0 or float(no2_peak) <= matched_max)
        and network_no2_rise >= 10.0
    ):
        supports_local_plume = "false"
    else:
        supports_local_plume = "indeterminate"

    rule = (
        "false when (matched_hour_z < 2.0 or event NO2 peak <= the preceding "
        "14-day matched-hour maximum) and the other-17-station mean NO2 rise "
        "is >= 10 ug/m3; true only when z >= 2.0, the event peak exceeds the "
        "preceding maximum, the network mean rise is < 5 ug/m3, and the nearest-"
        "station PM2.5 excess is >= 5 ug/m3; otherwise indeterminate"
    )

    return {
        "raw_ratio": raw_ratio,
        "raw_ratio_pollutant": "NO2",
        "raw_ratio_by_pollutant": {
            "NO2": raw_ratio,
            "PM2.5": pm25_raw_ratio,
        },
        "matched_hour_z": z,
        "matched_hour_mean": matched_mean,
        "matched_hour_sd": matched_sd,
        "matched_hour_n": len(matched_values),
        "matched_hour_max_prev_14d": matched_max,
        "network_mean_rise": network_no2_rise,
        "network_mean_rise_pollutant": "NO2",
        "network_mean_rise_other_17": network_no2_rise,
        "network_mean_rise_all_18": _mean(
            [
                float(row["no2_rise"])
                for row in network_rows
                if row.get("no2_rise") is not None
            ]
        ),
        "nearest_station_no2_rise": tai_no2_rise,
        "nearest_station_pm25_rise": tai_pm25_rise,
        "network_pm25_mean_rise_other_17": network_pm25_rise,
        "nearest_station_rise_vs_network": pm25_excess,
        "nearest_station_rise_vs_network_pollutant": "PM2.5",
        "pm25_rank_of_nearest": pm25_rank,
        "pm25_station_count": len(pm25_rise_values),
        "event_evening": [EVENT_EVENING_START, EVENT_EVENING_END],
        "baseline_window": ["2025-11-12", "2025-11-25"],
        "supports_local_plume": supports_local_plume,
        "rule": rule,
        "caveat": (
            "The peak-to-mean ratio is a screening signal, not causal "
            "attribution. The matched-hour z-score, the territory-wide network "
            "contrast and the near-calm wind do not support a single-source "
            "plume."
        ),
    }


def compute_reference(engine: Engine) -> dict[str, Any]:
    """Run the fixed read-only queries and return a JSON-serialisable report."""
    params = {
        "station_code": STATION_CODE,
        "peak_hour": PEAK_HOUR,
        "pre_start": PRE_START,
        "pre_end": PRE_END,
        "post_start": POST_START,
        "post_end": POST_END,
        "baseline_start": BASELINE_START,
        "baseline_end": BASELINE_END,
        "event_start": EVENT_EVENING_START,
        "event_end": EVENT_EVENING_END,
        "matched_hour": 20,
        "lat": FIRE_SITE_LAT,
        "lon": FIRE_SITE_LON,
    }

    with engine.connect() as conn:
        # Enforce read-only even if a caller passes an admin engine by mistake.
        conn.execute(text("SET TRANSACTION READ ONLY"))

        metrics = dict(
            conn.execute(METRICS_SQL, params).mappings().one()
        )
        matched_hour_rows = [
            dict(row) for row in conn.execute(MATCHED_HOUR_SQL, params).mappings()
        ]
        network_rows = [
            dict(row) for row in conn.execute(EVENING_NETWORK_SQL, params).mappings()
        ]
        wind = conn.execute(WIND_SQL, params).mappings().one_or_none() or {}
        station = conn.execute(STATION_SQL, params).mappings().one()

        fire_counts = [
            dict(row)
            for row in conn.execute(
                SENSITIVE_COUNTS_SQL,
                {**params, "radius_m": 1500},
            ).mappings()
        ]
        station_counts = [
            dict(row)
            for row in conn.execute(
                SENSITIVE_COUNTS_SQL,
                {
                    **params,
                    "lat": station["lat"],
                    "lon": station["lon"],
                    "radius_m": 1500,
                },
            ).mappings()
        ]
        nearest = [
            dict(row)
            for row in conn.execute(
                NEAREST_SENSITIVE_SQL,
                {**params, "radius_m": 1500, "limit": 5},
            ).mappings()
        ]
        buildings = dict(conn.execute(BUILDINGS_SQL, params).mappings().one())

        conn.rollback()

    observed: dict[str, Any] = {
        "no2_at_2000": _number(metrics["no2_at_2000"]),
        "aqhi_at_2000": metrics["aqhi_at_2000"],
        "pm25_post_peak": _number(metrics["pm25_post_peak"]),
        "pm25_pre_mean": _number(metrics["pm25_pre_mean"]),
        "no2_post_peak": _number(metrics["no2_post_peak"]),
        "no2_pre_mean": _number(metrics["no2_pre_mean"]),
        "wind_speed_2000": _number(wind.get("wind_speed")),
        "wind_degree_2000": _number(wind.get("wind_degree")),
        "wind_direction_2000": wind.get("wind_direction"),
        "sensitive_education_fire_1500": _category_count(
            fire_counts, "EDUCATIONAL_INSTITUTION"
        ),
        "sensitive_health_fire_1500": _category_count(
            fire_counts, "HEALTH_CARE_INSTITUTION"
        ),
        "sensitive_worship_fire_1500": _category_count(
            fire_counts, "PLACE_OF_WORSHIP"
        ),
        "sensitive_education_station_1500": _category_count(
            station_counts, "EDUCATIONAL_INSTITUTION"
        ),
        "buildings_2km": int(buildings["buildings_2km"]),
        "residential_1500": int(buildings["residential_1500"]),
        "buildings_25km": int(buildings["buildings_25km"]),
        # Extra deterministic context; not part of the pass/fail snapshot.
        "pm25_at_2000": _number(metrics["pm25_at_2000"]),
        "pm25_post_mean": _number(metrics["pm25_post_mean"]),
        "pm25_post_n": int(metrics["pm25_post_n"]),
        "no2_post_mean": _number(metrics["no2_post_mean"]),
        "no2_post_n": int(metrics["no2_post_n"]),
        "aqhi_post_peak": metrics["aqhi_post_peak"],
        "wind_gust_2000": _number(wind.get("wind_gust")),
        "temperature_2000": _number(wind.get("temperature")),
        "humidity_2000": _number(wind.get("humidity")),
        "pressure_2000": _number(wind.get("pressure")),
        "sensitive_worship_station_1500": _category_count(
            station_counts, "PLACE_OF_WORSHIP"
        ),
        "sensitive_health_station_1500": _category_count(
            station_counts, "HEALTH_CARE_INSTITUTION"
        ),
    }

    anomaly_assessment = _build_anomaly_assessment(
        observed, matched_hour_rows, network_rows
    )
    observed.update(
        {
            "matched_hour_z": anomaly_assessment["matched_hour_z"],
            "matched_hour_max_prev_14d": anomaly_assessment[
                "matched_hour_max_prev_14d"
            ],
            "network_mean_rise": anomaly_assessment["network_mean_rise"],
            "nearest_station_rise_vs_network": anomaly_assessment[
                "nearest_station_rise_vs_network"
            ],
            "pm25_rank_of_nearest": anomaly_assessment["pm25_rank_of_nearest"],
        }
    )

    checks = evaluate_reference(observed)
    ok = all(check.ok for check in checks)
    nearest_clean = [
        {
            "name": row["name"],
            "category": row["category"],
            "distance_m": round(float(row["distance_m"]), 1),
        }
        for row in nearest
    ]

    return {
        "name": "tai_po_fire_reference",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": ok,
        "degraded": not ok,
        "station_code": STATION_CODE,
        "fire_site": {
            "latitude": FIRE_SITE_LAT,
            "longitude": FIRE_SITE_LON,
            "label": "Wang Fuk Court, Tai Po",
        },
        "windows": {
            "peak_hour": PEAK_HOUR,
            "pre_fire": [PRE_START, PRE_END],
            "post_fire_24h": [POST_START, POST_END],
        },
        "thresholds": {
            "hk_aqo_pm25_24h": HK_AQO_PM25_24H,
            "hk_aqo_no2_1h": HK_AQO_NO2_1H,
            "who_pm25_24h": WHO_PM25_24H,
            "who_no2_24h": WHO_NO2_24H,
            "unit": "ug/m3",
            "note": (
                "The 54.6 ug/m3 PM2.5 value is an hourly peak inside the 24 h "
                "window; comparing it with a 24 h AQO or WHO guideline is a "
                "screening comparison, not a legal compliance determination."
            ),
        },
        "observed": observed,
        "expected": EXPECTED,
        "anomaly_assessment": anomaly_assessment,
        "checks": [asdict(check) for check in checks],
        "nearest_sensitive": nearest_clean,
        "notes": [
            "Read-only database reference; no LLM and no database writes.",
            "The peak-to-mean ratio is a screening signal, not causal attribution. "
            "The matched-hour z-score, the territory-wide network contrast and the "
            "near-calm wind do not support a single-source plume.",
            "Matched-hour baseline: Tai Po NO2 at 20:00 on 2025-11-26 against the "
            "same clock hour on the 14 preceding days (2025-11-12 to 2025-11-25), "
            "using the sample standard deviation (ddof=1).",
            "Network contrast: each station's mean over the event evening "
            "(2025-11-26 18:00-23:00) minus its mean over the same clock-hours on "
            "the 14 preceding days; the network mean uses the other 17 stations.",
            "OSM building counts are proximity-screening counts from the ingested "
            "2026 snapshot, not the paper's 1,090 total / 293 residential numbers.",
            "The fire-site-centred school count is 45; the station-centred "
            "check_agent truth is 53. Both are recorded so the difference is explicit.",
            "The wind value is near-calm (0.389 m/s from 207 degrees/SW), consistent "
            "with the Tolo basin narrative rather than a strong ventilation case.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit one-line JSON instead of pretty JSON",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional path to write the JSON report (for docs/tai_po_reference.json)",
    )
    args = parser.parse_args(argv)

    load_agent_env(override=True, scrub=True)
    engine = make_ro_engine()
    try:
        report = compute_reference(engine)
    finally:
        engine.dispose()

    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        default=str,
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
