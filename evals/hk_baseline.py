"""Day 4 single-agent baseline: 15 hand-verified questions, 5 categories.

Each case has a natural-language question, the SQL that produces the ground
truth, and the expected value pinned from an independent run on the ingested
data. The runner (`scripts/check_day4.py`) recomputes the truth and fails loudly
if the pinned value and the database disagree, so a silent data change cannot
turn into a silently wrong benchmark.

Categories (3 cases each):
  retrieval   one value / metadata / coverage count
  temporal    month-over-month, weekday-weekend, seasonal
  spatial     nearest station, radius count, station-to-station distance
  statistics  correlation, percentile, daily-mean exceedance count
  comparison  roadside-vs-general, cross-station ranking and differences

The values are for the 2025 ingest (2025-01-01 .. 2025-12-31). The AQHI data
was repaired so EPD's "10+" is stored as aqhi = 11 and labelled SERIOUS; case 3
is sensitive to that fix.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: int
    category: str
    label: str
    question: str
    truth_sql: str
    expected: float
    kind: str = "float"  # float | int


ANSWER_HINT = (
    "\n\nWhen you have the final answer, assign the numeric value to a variable named "
    "ANSWER (e.g. `ANSWER = 12.34`) and print it with `print(ANSWER)`. Then reply with "
    "one short sentence that includes the value and no code blocks."
)


_AQ = (
    "hourly_station_air_quality a "
    "JOIN air_quality_station s ON s.id = a.station_id"
)
_YEAR = "a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'"


CASES: tuple[Case, ...] = (
    # ------------------------------------------------------------------ retrieval
    Case(
        1,
        "retrieval",
        "latest-day PM2.5 (Tai Po)",
        "What is the mean PM2.5 concentration in ug/m3 at the station named 'Tai Po' "
        "on the most recent date for which the database has data? Treat that day as "
        "00:00 to 23:59 local time and round to 2 decimals.",
        "WITH last_day AS (SELECT max(datetime)::date AS d FROM hourly_station_air_quality) "
        "SELECT round(avg(a.pm2_5)::numeric, 2) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id, last_day "
        "WHERE s.name_en = 'Tai Po' "
        "AND a.datetime >= last_day.d AND a.datetime < last_day.d + 1",
        21.60,
    ),
    Case(
        2,
        "retrieval",
        "station altitude (Tap Mun)",
        "What is the altitude in metres of the air quality station named 'Tap Mun'? "
        "Round to 1 decimal.",
        "SELECT round(altitude::numeric, 1) FROM air_quality_station WHERE name_en = 'Tap Mun'",
        30.0,
    ),
    Case(
        3,
        "retrieval",
        "SERIOUS station-hours in 2025",
        "How many hourly air quality rows in 2025 (2025-01-01 to 2025-12-31) have "
        "aqhi_level = 'SERIOUS'? Answer with a whole number.",
        f"SELECT count(*) FROM {_AQ} "
        f"WHERE a.aqhi_level = 'SERIOUS' AND {_YEAR}",
        47,
        "int",
    ),
    # ------------------------------------------------------------------- temporal
    Case(
        4,
        "temporal",
        "NO2 July minus January (Causeway Bay)",
        "At the station named 'Causeway Bay', what is the mean NO2 in July 2025 minus "
        "the mean NO2 in January 2025, in ug/m3? Round to 2 decimals.",
        "WITH monthly AS ("
        "  SELECT extract(month FROM a.datetime) AS month, avg(a.no2) AS value "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Causeway Bay' "
        "  AND a.datetime >= '2025-01-01' AND a.datetime < '2025-08-01' "
        "  GROUP BY 1) "
        "SELECT round((max(value) FILTER (WHERE month = 7) "
        "            - max(value) FILTER (WHERE month = 1))::numeric, 2) FROM monthly",
        -23.08,
    ),
    Case(
        5,
        "temporal",
        "PM2.5 weekday minus weekend (Causeway Bay, Nov)",
        "At the station named 'Causeway Bay' in November 2025, what is the mean PM2.5 "
        "on weekdays (Monday-Friday) minus the mean PM2.5 on weekends (Saturday-Sunday), "
        "in ug/m3? Round to 2 decimals.",
        "SELECT round((avg(a.pm2_5) FILTER (WHERE extract(isodow FROM a.datetime) < 6) "
        "            - avg(a.pm2_5) FILTER (WHERE extract(isodow FROM a.datetime) >= 6))::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Causeway Bay' "
        "AND a.datetime >= '2025-11-01' AND a.datetime < '2025-12-01'",
        -5.64,
    ),
    Case(
        6,
        "temporal",
        "PM2.5 December minus June (Tung Chung)",
        "At the station named 'Tung Chung', what is the mean PM2.5 in December 2025 "
        "minus the mean PM2.5 in June 2025, in ug/m3? Round to 2 decimals.",
        "WITH monthly AS ("
        "  SELECT extract(month FROM a.datetime) AS month, avg(a.pm2_5) AS value "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Tung Chung' "
        "  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  GROUP BY 1) "
        "SELECT round((max(value) FILTER (WHERE month = 12) "
        "            - max(value) FILTER (WHERE month = 6))::numeric, 2) FROM monthly",
        19.52,
    ),
    # -------------------------------------------------------------------- spatial
    Case(
        7,
        "spatial",
        "nearest station to HKU (km)",
        "How far, in kilometres, is the nearest air quality station from the point "
        "22.2833 N, 114.1375 E (The University of Hong Kong)? Round to 2 decimals.",
        "SELECT round((min(ST_Distance(coordinate, "
        "  ST_SetSRID(ST_MakePoint(114.1375, 22.2833), 4326)::geography)) / 1000.0)::numeric, 2) "
        "FROM air_quality_station",
        1.29,
    ),
    Case(
        8,
        "spatial",
        "places of worship within 2 km (Yuen Long)",
        "How many rows in sensitive_area with category 'PLACE_OF_WORSHIP' lie within "
        "2000 metres of the air quality station named 'Yuen Long'? Distances must be "
        "computed in metres. Answer with a whole number.",
        "SELECT count(*) FROM sensitive_area sa, air_quality_station s "
        "WHERE s.name_en = 'Yuen Long' AND sa.category = 'PLACE_OF_WORSHIP' "
        "AND ST_DWithin(sa.geometry::geography, s.coordinate, 2000)",
        9,
        "int",
    ),
    Case(
        9,
        "spatial",
        "Tai Po to Sha Tin distance (km)",
        "What is the distance in kilometres between the air quality stations named "
        "'Tai Po' and 'Sha Tin'? Round to 2 decimals.",
        "SELECT round((ST_Distance(a.coordinate, b.coordinate) / 1000.0)::numeric, 2) "
        "FROM air_quality_station a, air_quality_station b "
        "WHERE a.name_en = 'Tai Po' AND b.name_en = 'Sha Tin'",
        8.59,
    ),
    # ----------------------------------------------------------------- statistics
    Case(
        10,
        "statistics",
        "PM2.5/NO2 correlation (Causeway Bay, Nov)",
        "What is the Pearson correlation between hourly PM2.5 and hourly NO2 at the "
        "station named 'Causeway Bay' in November 2025, using pairwise complete "
        "observations? Round to 2 decimals.",
        "SELECT round(corr(a.pm2_5, a.no2)::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Causeway Bay' "
        "AND a.datetime >= '2025-11-01' AND a.datetime < '2025-12-01'",
        0.56,
    ),
    Case(
        11,
        "statistics",
        "95th percentile NO2 (Mong Kok, 2025)",
        "What is the 95th percentile (linear interpolation) of hourly NO2 at the "
        "station named 'Mong Kok' in 2025? Round to 2 decimals.",
        "SELECT round(percentile_cont(0.95) WITHIN GROUP (ORDER BY a.no2)::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Mong Kok' AND a.datetime >= '2025-01-01' "
        "AND a.datetime < '2026-01-01'",
        123.50,
    ),
    Case(
        12,
        "statistics",
        "days above the new PM2.5 AQO (Yuen Long)",
        "Between 2025-04-11 and 2025-12-31 inclusive, on how many days did the daily "
        "mean PM2.5 at the station named 'Yuen Long' exceed 37.5 ug/m3? A day's value "
        "is the mean of its hourly readings. Answer with a whole number.",
        "WITH daily AS ("
        "  SELECT a.datetime::date AS day, avg(a.pm2_5) AS value "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Yuen Long' "
        "  AND a.datetime >= '2025-04-11' AND a.datetime < '2026-01-01' "
        "  GROUP BY 1) "
        "SELECT count(*) FROM daily WHERE value > 37.5",
        8,
        "int",
    ),
    # ----------------------------------------------------------------- comparison
    Case(
        13,
        "comparison",
        "NO2 roadside minus general (2025)",
        "In 2025, what is the mean NO2 at ROADSIDE stations minus the mean NO2 at "
        "GENERAL stations, in ug/m3? Round to 2 decimals.",
        "SELECT round((avg(a.no2) FILTER (WHERE s.station_type = 'ROADSIDE') "
        "            - avg(a.no2) FILTER (WHERE s.station_type = 'GENERAL'))::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        32.32,
    ),
    Case(
        14,
        "comparison",
        "highest station-level annual mean O3",
        "Across all air quality stations, what is the highest 2025 annual mean O3 "
        "concentration in ug/m3? Round to 2 decimals.",
        "SELECT round(max(value)::numeric, 2) FROM ("
        "  SELECT avg(a.o3) AS value "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  GROUP BY s.id) AS per_station",
        78.84,
    ),
    Case(
        15,
        "comparison",
        "O3 Tap Mun minus Causeway Bay (2025)",
        "What is the difference in 2025 annual mean O3 between the stations named "
        "'Tap Mun' and 'Causeway Bay' (Tap Mun minus Causeway Bay), in ug/m3? Round "
        "to 2 decimals.",
        "SELECT round((avg(a.o3) FILTER (WHERE s.name_en = 'Tap Mun') "
        "            - avg(a.o3) FILTER (WHERE s.name_en = 'Causeway Bay'))::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        47.91,
    ),
)
