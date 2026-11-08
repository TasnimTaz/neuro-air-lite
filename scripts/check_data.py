"""Day 2 acceptance checks for the Hong Kong data.

    uv run python scripts/check_data.py

Runs against the read-only role, so it proves the data is reachable exactly the way
the agent will reach it. Every check prints `[ ok ]`, `[warn]` or `[FAIL]`; warnings
describe known properties of the sources, failures mean the ingest is wrong.

What it proves:

* every table the plan names for Day 2 is populated, and the still-empty ones are
  listed explicitly rather than silently ignored;
* no duplicate (station, datetime) pairs, and the hourly coverage is dense enough
  for the later days (>= 14 days per station, the plan's minimum);
* the physical ranges of every pollutant and weather column are plausible, so a
  unit error (CO in mg/m3, wind in km/h) shows up as a failure instead of a silently
  wrong model input;
* the geometries really are SRID 4326 and the radius helpers cast to geography;
* the paper's Tai Po sanity values reproduce, which pins both the data source and
  the timezone convention.

Idempotency is checked by re-running an ingest and comparing counts, not here:
`uv run python -m ingest.aq && uv run python scripts/check_data.py` must print the
same row counts as the first run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sqlalchemy import text

from domains.hk.db import make_ro_engine
from domains.hk.stations import BY_CODE, weather_code

EXPECTED_TABLES = [
    "air_quality_station",
    "hourly_station_air_quality",
    "street",
    "road_segment",
    "hourly_road_segment_traffic",
    "weather_station",
    "hourly_station_weather",
    "osm_feature",
    "eia_project",
    "eia_habitat",
    "traffic_camera",
    "sensitive_area",
    "land_use_zone",
    "building_height_control",
]

# Tables Day 2 is responsible for. Anything else stays empty until its own day.
DAY2_TABLES = [
    "air_quality_station",
    "hourly_station_air_quality",
    "weather_station",
    "hourly_station_weather",
    "osm_feature",
    "sensitive_area",
]

STATION_COUNT = 18
ROADSIDE_COUNT = 3
HOURS_IN_2025 = 8760
MIN_DAYS = 14

# column -> (low, high) in the units the schema documents (ug/m3, m/s, degC, ...).
AQ_RANGES = {
    "aqhi": (1, 11),
    "pm2_5": (0, 500),
    "pm10": (0, 600),
    "o3": (0, 500),
    "so2": (0, 1000),
    "no2": (0, 1000),
    "co": (0, 10000),
}

WX_RANGES = {
    "temperature": (-5, 45),
    "humidity": (0, 100),
    "pressure": (950, 1050),
    "wind_speed": (0, 60),
    "wind_gust": (0, 80),
    "wind_degree": (0, 360),
}

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"[ ok ] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    failures.append(msg)


def warn(msg: str) -> None:
    print(f"[warn] {msg}")


def pgcode(exc: Exception) -> str | None:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def scalar(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def check_tables(conn) -> None:
    print("-- tables --")
    present = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
    }
    missing = [t for t in EXPECTED_TABLES if t not in present]
    if missing:
        fail(f"missing tables: {', '.join(missing)}")
    else:
        ok(f"all {len(EXPECTED_TABLES)} expected tables present")

    empty = []
    for table in EXPECTED_TABLES:
        count = scalar(conn, f'SELECT count(*) FROM "{table}"')
        state = "empty" if count == 0 else f"{count:,}"
        print(f"       {table:<32} {state:>10}")
        if count == 0:
            empty.append(table)

    day2_empty = [t for t in DAY2_TABLES if t in empty]
    if day2_empty:
        fail(f"Day 2 tables still empty: {', '.join(day2_empty)}")
    else:
        ok(f"all {len(DAY2_TABLES)} Day 2 tables populated")

    later = [t for t in empty if t not in DAY2_TABLES]
    if later:
        warn(f"{len(later)} tables belong to later days and are still empty: {', '.join(later)}")


def check_duplicates(conn) -> None:
    print("\n-- primary keys --")
    for table in ("hourly_station_air_quality", "hourly_station_weather"):
        dupes = scalar(
            conn,
            f"SELECT count(*) FROM (SELECT station_id, datetime, count(*) AS n "
            f'FROM "{table}" GROUP BY station_id, datetime HAVING count(*) > 1) AS d',
        )
        if dupes:
            fail(f"{table}: {dupes} duplicated (station_id, datetime) pairs")
        else:
            ok(f"{table}: no duplicate (station_id, datetime)")


def check_air_quality(conn) -> None:
    print("\n-- air quality --")
    stations = scalar(conn, "SELECT count(*) FROM air_quality_station")
    roadside = scalar(
        conn, "SELECT count(*) FROM air_quality_station WHERE station_type = 'ROADSIDE'"
    )
    if stations == STATION_COUNT:
        ok(f"{stations} stations registered")
    else:
        fail(f"{stations} stations, expected {STATION_COUNT}")
    if roadside == ROADSIDE_COUNT:
        ok(f"{roadside} roadside stations")
    else:
        fail(f"{roadside} roadside stations, expected {ROADSIDE_COUNT}")

    first, last, total = conn.execute(
        text(
            "SELECT min(datetime), max(datetime), count(*) "
            "FROM hourly_station_air_quality"
        )
    ).one()
    print(f"       range {first} .. {last}, {total:,} rows")

    empty_stations = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT s.name_en FROM air_quality_station s "
                "LEFT JOIN hourly_station_air_quality a ON a.station_id = s.id "
                "GROUP BY s.name_en HAVING count(a.id) = 0 ORDER BY s.name_en"
            )
        )
    ]
    if empty_stations:
        fail(f"stations with no rows: {', '.join(empty_stations)}")
    else:
        ok("every station has rows")

    # Coverage per station: distinct hours over the requested year.
    thin = []
    for name, hours in conn.execute(
        text(
            "SELECT s.name_en, count(DISTINCT a.datetime) "
            "FROM hourly_station_air_quality a "
            "JOIN air_quality_station s ON s.id = a.station_id "
            "GROUP BY s.name_en ORDER BY s.name_en"
        )
    ):
        if hours < MIN_DAYS * 24:
            thin.append(f"{name} ({hours} h)")
        elif hours < HOURS_IN_2025 * 0.98:
            warn(f"{name}: {hours:,} of {HOURS_IN_2025:,} hours ({hours / HOURS_IN_2025:.1%})")
    if thin:
        fail(f"stations below {MIN_DAYS} days of data: {', '.join(thin)}")
    else:
        ok(f"every station has at least {MIN_DAYS} days of hourly data")

    for column, (low, high) in AQ_RANGES.items():
        present, nulls, bad_min, bad_max = conn.execute(
            text(
                f"SELECT count({column}), count(*) - count({column}), "
                f"count(*) FILTER (WHERE {column} < :low), "
                f"count(*) FILTER (WHERE {column} > :high) "
                "FROM hourly_station_air_quality"
            ),
            {"low": low, "high": high},
        ).one()
        if present == 0:
            fail(f"{column}: no values at all")
            continue
        if bad_min or bad_max:
            fail(f"{column}: {bad_min + bad_max} values outside [{low}, {high}]")
        else:
            ok(f"{column}: {present:,} values in [{low}, {high}], {nulls:,} null")

    bad_levels = scalar(
        conn,
        "SELECT count(*) FROM hourly_station_air_quality WHERE "
        "  (aqhi IS NOT NULL AND aqhi_level IS NULL) "
        "  OR (aqhi <= 3 AND aqhi_level <> 'LOW') "
        "  OR (aqhi BETWEEN 4 AND 6 AND aqhi_level <> 'MODERATE') "
        "  OR (aqhi = 7 AND aqhi_level <> 'HIGH') "
        "  OR (aqhi BETWEEN 8 AND 10 AND aqhi_level <> 'VERY HIGH') "
        "  OR (aqhi > 10 AND aqhi_level <> 'SERIOUS')",
    )
    if bad_levels:
        fail(f"{bad_levels} rows where aqhi_level disagrees with aqhi")
    else:
        ok("aqhi_level matches aqhi on every row")


def check_weather(conn) -> None:
    print("\n-- weather --")
    stations = scalar(conn, "SELECT count(*) FROM weather_station")
    if stations == STATION_COUNT:
        ok(f"{stations} weather stations registered")
    else:
        fail(f"{stations} weather stations, expected {STATION_COUNT}")

    first, last, total = conn.execute(
        text("SELECT min(datetime), max(datetime), count(*) FROM hourly_station_weather")
    ).one()
    print(f"       range {first} .. {last}, {total:,} rows")

    for column, (low, high) in WX_RANGES.items():
        present, nulls, bad_min, bad_max = conn.execute(
            text(
                f"SELECT count({column}), count(*) - count({column}), "
                f"count(*) FILTER (WHERE {column} < :low), "
                f"count(*) FILTER (WHERE {column} > :high) "
                "FROM hourly_station_weather"
            ),
            {"low": low, "high": high},
        ).one()
        if present == 0:
            fail(f"{column}: no values at all")
            continue
        if bad_min or bad_max:
            fail(f"{column}: {bad_min + bad_max} values outside [{low}, {high}]")
        else:
            ok(f"{column}: {present:,} values in [{low}, {high}], {nulls:,} null")

    # A gust is the maximum of the ten-minute maxima, so it cannot be below the
    # hourly mean. Tiny floating point inversions are tolerated.
    bad_gust = scalar(
        conn,
        "SELECT count(*) FROM hourly_station_weather "
        "WHERE wind_gust IS NOT NULL AND wind_speed IS NOT NULL "
        "AND wind_gust + 1e-6 < wind_speed",
    )
    if bad_gust:
        warn(f"{bad_gust} hours where wind_gust < wind_speed (rounding)")
    else:
        ok("wind_gust >= wind_speed on every row")

    thin = [
        f"{name} ({hours} h)"
        for name, hours in conn.execute(
            text(
                "SELECT s.name_en, count(DISTINCT w.datetime) "
                "FROM hourly_station_weather w "
                "JOIN weather_station s ON s.id = w.station_id "
                "WHERE w.wind_speed IS NOT NULL "
                "GROUP BY s.name_en ORDER BY s.name_en"
            )
        )
        if hours < MIN_DAYS * 24
    ]
    if thin:
        fail(f"stations below {MIN_DAYS} days of wind: {', '.join(thin)}")
    else:
        ok(f"every station has at least {MIN_DAYS} days of wind")


def check_osm(conn) -> None:
    print("\n-- openstreetmap --")
    total = scalar(conn, "SELECT count(*) FROM osm_feature")
    if total == 0:
        fail("osm_feature is empty")
        return
    ok(f"osm_feature: {total:,} rows")

    bad_srid = scalar(
        conn, "SELECT count(*) FROM osm_feature WHERE ST_SRID(geometry) <> 4326"
    )
    if bad_srid:
        fail(f"{bad_srid} osm_feature geometries not in SRID 4326")
    else:
        ok("all osm_feature geometries are SRID 4326")

    for main_type, count in conn.execute(
        text("SELECT main_type, count(*) FROM osm_feature GROUP BY main_type ORDER BY 2 DESC")
    ):
        print(f"       {main_type:<16} {count:>7,}")

    for geometry_type, count in conn.execute(
        text(
            "SELECT geometry_type, count(*) FROM osm_feature "
            "GROUP BY geometry_type ORDER BY 2 DESC"
        )
    ):
        print(f"       {geometry_type:<16} {count:>7,}")

    areas = scalar(conn, "SELECT count(*) FROM sensitive_area")
    if areas == 0:
        warn("sensitive_area is empty (schools/hospitals live only in osm_feature)")
    else:
        ok(f"sensitive_area: {areas:,} rows")
        for category, count in conn.execute(
            text(
                "SELECT category, count(*) FROM sensitive_area "
                "GROUP BY category ORDER BY 2 DESC"
            )
        ):
            print(f"       {category:<24} {count:>7,}")

    # The prompt tells the model to cast geometry to geography before ST_DWithin.
    # Prove the cast yields metres, not degrees.
    metres = scalar(
        conn,
        "SELECT ST_Distance("
        "ST_SetSRID(ST_MakePoint(114.1754, 22.4468), 4326)::geography, "
        "ST_SetSRID(ST_MakePoint(114.1754, 22.4568), 4326)::geography)",
    )
    if 1000 < metres < 1200:
        ok(f"geometry::geography distance is in metres ({metres:.0f} m per 0.01 deg)")
    else:
        fail(f"geometry::geography distance looks wrong: {metres}")


def check_taipo_spatial(conn) -> None:
    print("\n-- Tai Po vicinity spatial checks (informational) --")
    nearest = conn.execute(
        text(
            "SELECT name, category, "
            "ST_Distance(geometry::geography, ST_SetSRID(ST_MakePoint(114.1754, 22.4468), 4326)::geography) AS dist_m "
            "FROM sensitive_area "
            "ORDER BY dist_m ASC LIMIT 5"
        )
    ).fetchall()
    if nearest:
        for r in nearest:
            print(f"       {r[0] or '(unnamed)':<36} {r[1]:<24} {r[2]:>6.1f} m")
        nearest_name = nearest[0][0] or "(unnamed)"
        nearest_dist = nearest[0][2]
        warn(f"nearest sensitive_area: '{nearest_name}' at {nearest_dist:.1f} m (paper: Tai Po Baptist Public School ~35 m)")
    else:
        warn("no sensitive_area rows found")

    bldgs_2km = scalar(
        conn,
        "SELECT count(*) FROM osm_feature "
        "WHERE main_type = 'BUILDING' "
        "AND ST_DWithin(geometry::geography, ST_SetSRID(ST_MakePoint(114.1754, 22.4468), 4326)::geography, 2000)",
    )
    res_15km = scalar(
        conn,
        "SELECT count(*) FROM osm_feature "
        "WHERE main_type = 'BUILDING' "
        "AND sub_type IN ('residential', 'apartments', 'house', 'terrace', 'dormitory') "
        "AND ST_DWithin(geometry::geography, ST_SetSRID(ST_MakePoint(114.1754, 22.4468), 4326)::geography, 1500)",
    )
    print(f"       buildings <= 2 km: {bldgs_2km:,} (paper: 1,090), residential <= 1.5 km: {res_15km:,} (paper: 293)")
    warn(f"buildings <= 2 km: {bldgs_2km:,} (paper: 1,090), residential <= 1.5 km: {res_15km:,} (paper: 293)")


def check_paper_sanity(conn) -> None:
    """The values the plan gives for the Tai Po case study."""
    print("\n-- paper sanity (Tai Po) --")
    row = conn.execute(
        text(
            "SELECT a.datetime, a.no2, a.o3, a.so2, a.co, a.pm10, a.pm2_5, a.aqhi "
            "FROM hourly_station_air_quality a "
            "JOIN air_quality_station s ON s.id = a.station_id "
            "WHERE s.name_en = 'Tai Po' AND a.datetime = '2025-11-26 20:00'"
        )
    ).first()
    if row is None:
        fail("Tai Po 2025-11-26 20:00 is missing")
        return

    _, no2, o3, so2, co, pm10, pm2_5, aqhi = row
    if no2 is not None and abs(no2 - 123.2) < 0.05:
        ok(f"NO2 = {no2} ug/m3 (paper: 123.2)")
    else:
        fail(f"NO2 = {no2}, expected 123.2 (source or timezone is wrong)")

    print(f"       O3={o3} SO2={so2} CO={co} PM10={pm10} PM2.5={pm2_5} AQHI={aqhi}")

    peak = conn.execute(
        text(
            "SELECT a.pm2_5, a.datetime FROM hourly_station_air_quality a "
            "JOIN air_quality_station s ON s.id = a.station_id "
            "WHERE s.name_en = 'Tai Po' AND a.pm2_5 IS NOT NULL "
            "ORDER BY a.pm2_5 DESC LIMIT 1"
        )
    ).first()
    if peak is None:
        fail("Tai Po has no PM2.5 values")
    elif abs(peak[0] - 54.6) < 0.05:
        ok(f"PM2.5 peak = {peak[0]} ug/m3 at {peak[1]} (paper: 54.6)")
    else:
        warn(f"PM2.5 peak = {peak[0]} ug/m3 at {peak[1]}, paper quotes 54.6")

    tai_po_wx = weather_code(BY_CODE["taipo"])
    wind = conn.execute(
        text(
            "SELECT w.datetime, w.wind_speed, w.wind_degree, w.wind_direction "
            "FROM hourly_station_weather w JOIN weather_station s ON s.id = w.station_id "
            "WHERE s.station_code = :wx_code AND w.datetime = '2025-11-26 20:00'"
        ),
        {"wx_code": tai_po_wx},
    ).first()
    if wind is None:
        warn(f"Tai Po 2025-11-26 20:00 has no weather row yet (code: {tai_po_wx})")
    else:
        print(f"       Tai Po wind at 20:00: {wind[1]} m/s from {wind[2]:.0f} deg ({wind[3]})")


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    timeout_ms = int(os.environ.get("STATEMENT_TIMEOUT_MS", "30000"))

    print("=== neuro-air-lite :: Day 2 data check ===\n")
    engine = make_ro_engine(statement_timeout_ms=timeout_ms)
    try:
        with engine.connect() as conn:
            check_tables(conn)
            check_duplicates(conn)
            check_air_quality(conn)
            check_weather(conn)
            check_osm(conn)
            check_taipo_spatial(conn)
            check_paper_sanity(conn)
    except Exception as exc:  # noqa: BLE001
        fail(f"query failed ({pgcode(exc) or exc.__class__.__name__}): {exc}")
    finally:
        engine.dispose()

    print()
    if failures:
        print(f"RESULT: {len(failures)} check(s) failed")
        return 1
    print("RESULT: Day 2 data ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
