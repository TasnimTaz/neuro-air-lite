"""Day 4 baseline v2: 20 questions phrased like a real user.

This set replaces `hk_baseline.py` as the Day 9 single-agent reference. The v1
set is kept as a contaminated smoke test: three of its questions were also
worked examples in the prompt, so its 13/15 and 15/15 runs measured prompt
memorisation as much as reasoning.

Design rules for v2:

* no station codes, unit instructions or rounding instructions in the question;
  the tolerance lives in `Case`, and the ANSWER hint is harness plumbing;
* at least ten cases need two or more tables (or a spatial join);
* four fuzzy entity names force an explicit lookup (``Shatin``, ``tung chung``,
  ``causeway bay``, ``Central/Western``);
* two NULL traps: CO at a station that does not measure it, and a real
  station-hour whose PM2.5 is NULL. Answering 0 is a fail; "not available" is a
  pass;
* two invalid premises: a station that does not exist and a 2026 date. The only
  pass is to say the question cannot be answered and leave ANSWER unset;
* six hold-out cases are marked and are not used to tune the prompt. The whole
  set is frozen before the first run; no prompt iteration is allowed afterwards.

Truth values are pinned from a fresh query on the admin engine. The runner
recomputes every truth value before scoring and fails loudly if the data moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Case:
    id: int
    category: str
    difficulty: str
    question: str
    truth_sql: str
    expected: Any
    kind: str  # float | int | text | unanswerable
    tolerance: float = 0.0
    holdout: bool = False
    note: str = ""


ANSWER_HINT = (
    "\n\nWhen you have the final answer, assign it to a variable named ANSWER "
    "(a number for numeric answers, a short string for text answers) and print it "
    "with `print(ANSWER)`. If the question cannot be answered from the data, leave "
    "ANSWER unset and say why. Then reply with one short sentence and no code blocks."
)


CASES: tuple[Case, ...] = (
    # ------------------------------------------------------------------ retrieval
    Case(
        1,
        "retrieval",
        "simple",
        "How high above sea level is the Tseung Kwan O air quality station?",
        "SELECT round(altitude::numeric, 1) FROM air_quality_station "
        "WHERE name_en = 'Tseung Kwan O'",
        20.0,
        "float",
        0.1,
    ),
    Case(
        2,
        "retrieval",
        "simple",
        "How many general air quality stations are in new-town areas?",
        "SELECT count(*) FROM air_quality_station "
        "WHERE station_type = 'GENERAL' AND area_type = 'NEW_TOWN'",
        7,
        "int",
        holdout=True,
    ),
    Case(
        3,
        "retrieval",
        "medium",
        "What was the average CO concentration at the Sha Tin air quality station in 2025?",
        "SELECT avg(a.co) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Sha Tin' "
        "AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        None,
        "text",
        note="CO is NULL at all Sha Tin rows: the only correct answer is 'not available'.",
    ),
    Case(
        4,
        "retrieval",
        "simple",
        "What was the average PM2.5 at the Kennedy Town air quality station in 2025?",
        "SELECT count(*) FROM air_quality_station "
        "WHERE name_en ILIKE '%Kennedy Town%'",
        None,
        "unanswerable",
        note="No such station in the 18-station inventory.",
    ),
    # ------------------------------------------------------------------- temporal
    Case(
        5,
        "temporal",
        "medium",
        "At Shatin, how much warmer was August than January 2025 on average?",
        "WITH monthly AS ("
        "  SELECT extract(month FROM w.datetime) AS month, avg(w.temperature) AS t "
        "  FROM hourly_station_weather w JOIN weather_station s ON s.id = w.station_id "
        "  WHERE s.station_code = 'wx_shatin' "
        "  AND w.datetime >= '2025-01-01' AND w.datetime < '2025-09-01' "
        "  GROUP BY 1) "
        "SELECT round((max(t) FILTER (WHERE month = 8) "
        "            - max(t) FILTER (WHERE month = 1))::numeric, 2) FROM monthly",
        12.40,
        "float",
        0.2,
        note="Fuzzy name: 'Shatin' must resolve to the Sha Tin station/weather pair.",
    ),
    Case(
        6,
        "temporal",
        "medium",
        "At Tung Chung, how much higher is the average PM2.5 on weekends than on "
        "weekdays in 2025?",
        "SELECT round((avg(a.pm2_5) FILTER (WHERE extract(isodow FROM a.datetime) >= 6) "
        "            - avg(a.pm2_5) FILTER (WHERE extract(isodow FROM a.datetime) < 6))::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Tung Chung' "
        "AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        0.28,
        "float",
        0.1,
        holdout=True,
    ),
    Case(
        7,
        "temporal",
        "medium",
        "What was Yuen Long's highest monthly average PM2.5 in 2025?",
        "WITH monthly AS ("
        "  SELECT extract(month FROM a.datetime) AS month, avg(a.pm2_5) AS v "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Yuen Long' "
        "  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  GROUP BY 1) "
        "SELECT round(max(v)::numeric, 2) FROM monthly",
        30.16,
        "float",
        0.2,
    ),
    Case(
        8,
        "temporal",
        "simple",
        "What was the average PM2.5 at Tai Po in January 2026?",
        "SELECT count(*) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Tai Po' "
        "AND a.datetime >= '2026-01-01' AND a.datetime < '2026-02-01'",
        None,
        "unanswerable",
        note="The ingest ends on 2025-12-31; January 2026 is outside coverage.",
    ),
    # -------------------------------------------------------------------- spatial
    Case(
        9,
        "spatial",
        "medium",
        "How many schools are within 1 km of the Tsuen Wan air quality station?",
        "SELECT count(*) FROM sensitive_area sa, air_quality_station s "
        "WHERE s.name_en = 'Tsuen Wan' "
        "AND sa.category = 'EDUCATIONAL_INSTITUTION' "
        "AND ST_DWithin(sa.geometry::geography, s.coordinate, 1000)",
        21,
        "int",
    ),
    Case(
        10,
        "spatial",
        "medium",
        "How many schools are within 1.5 km of the Kwun Tong air quality station?",
        "SELECT count(*) FROM sensitive_area sa, air_quality_station s "
        "WHERE s.name_en = 'Kwun Tong' "
        "AND sa.category = 'EDUCATIONAL_INSTITUTION' "
        "AND ST_DWithin(sa.geometry::geography, s.coordinate, 1500)",
        51,
        "int",
    ),
    Case(
        11,
        "spatial",
        "hard",
        "Which air quality station is closest to Victoria Park in Causeway Bay?",
        "SELECT name_en FROM air_quality_station "
        "ORDER BY ST_Distance(coordinate, "
        "  ST_SetSRID(ST_MakePoint(114.1886, 22.2822), 4326)::geography) "
        "LIMIT 1",
        "Causeway Bay",
        "text",
        holdout=True,
        note="Requires geocode('Victoria Park') and a spatial ordering.",
    ),
    Case(
        12,
        "spatial",
        "medium",
        "How many hospitals are within 2 km of the Central/Western air quality station?",
        "SELECT count(*) FROM sensitive_area sa, air_quality_station s "
        "WHERE s.name_en = 'Central/Western' "
        "AND sa.category = 'HEALTH_CARE_INSTITUTION' "
        "AND ST_DWithin(sa.geometry::geography, s.coordinate, 2000)",
        4,
        "int",
    ),
    # ----------------------------------------------------------------- statistics
    Case(
        13,
        "statistics",
        "medium",
        "What is the 95th percentile of hourly PM2.5 at tung chung in 2025?",
        "SELECT round(percentile_cont(0.95) WITHIN GROUP (ORDER BY a.pm2_5)::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Tung Chung' "
        "AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        40.00,
        "float",
        0.2,
        note="Lower-case fuzzy name: 'tung chung'.",
    ),
    Case(
        14,
        "statistics",
        "hard",
        "At causeway bay, what is the correlation between hourly NO2 and wind speed "
        "in 2025?",
        "SELECT round(corr(a.no2, w.wind_speed)::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "JOIN weather_station ws ON ws.station_code = 'wx_' || s.station_code "
        "JOIN hourly_station_weather w ON w.station_id = ws.id AND w.datetime = a.datetime "
        "WHERE s.name_en = 'Causeway Bay' "
        "AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        -0.09,
        "float",
        0.05,
        holdout=True,
        note="Fuzzy name plus a three-table hourly join.",
    ),
    Case(
        15,
        "statistics",
        "medium",
        "How many days in 2025 did Tai Po's daily average PM2.5 exceed 30?",
        "WITH daily AS ("
        "  SELECT a.datetime::date AS day, avg(a.pm2_5) AS v "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Tai Po' "
        "  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  GROUP BY 1) "
        "SELECT count(*) FROM daily WHERE v > 30",
        31,
        "int",
    ),
    Case(
        16,
        "statistics",
        "medium",
        "What was the PM2.5 reading at the Tai Po air quality station at noon on "
        "7 January 2025?",
        "SELECT a.pm2_5 FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Tai Po' AND a.datetime = '2025-01-07 12:00:00'",
        None,
        "text",
        note="The row exists but pm2_5 is NULL: answering 0 is a fail.",
    ),
    # ----------------------------------------------------------------- multi-table
    Case(
        17,
        "multi_table",
        "hard",
        "On Tai Po's worst PM2.5 day in 2025, what was the average wind speed at its "
        "paired weather station?",
        "WITH daily AS ("
        "  SELECT a.datetime::date AS day, avg(a.pm2_5) AS v "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Tai Po' "
        "  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  GROUP BY 1), "
        "worst AS (SELECT day FROM daily ORDER BY v DESC NULLS LAST LIMIT 1) "
        "SELECT round(avg(w.wind_speed)::numeric, 2) "
        "FROM hourly_station_weather w "
        "JOIN weather_station ws ON ws.id = w.station_id "
        "JOIN worst ON w.datetime::date = worst.day "
        "WHERE ws.station_code = 'wx_taipo'",
        1.81,
        "float",
        0.1,
        holdout=True,
    ),
    Case(
        18,
        "multi_table",
        "hard",
        "During the five hours with the highest PM2.5 at Tai Po in 2025, what was "
        "the average wind speed at the paired weather station?",
        "WITH top5 AS ("
        "  SELECT a.datetime "
        "  FROM hourly_station_air_quality a "
        "  JOIN air_quality_station s ON s.id = a.station_id "
        "  WHERE s.name_en = 'Tai Po' "
        "  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  ORDER BY a.pm2_5 DESC NULLS LAST LIMIT 5) "
        "SELECT round(avg(w.wind_speed)::numeric, 2) "
        "FROM hourly_station_weather w "
        "JOIN weather_station ws ON ws.id = w.station_id "
        "JOIN top5 ON w.datetime = top5.datetime "
        "WHERE ws.station_code = 'wx_taipo'",
        4.24,
        "float",
        0.2,
    ),
    Case(
        19,
        "multi_table",
        "hard",
        "How many schools are within 1.5 km of the air quality station with the "
        "highest annual average NO2 in 2025?",
        "WITH best AS ("
        "  SELECT a.station_id "
        "  FROM hourly_station_air_quality a "
        "  WHERE a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "  GROUP BY 1 ORDER BY avg(a.no2) DESC NULLS LAST LIMIT 1) "
        "SELECT count(*) "
        "FROM sensitive_area sa, air_quality_station s, best "
        "WHERE s.id = best.station_id "
        "AND sa.category = 'EDUCATIONAL_INSTITUTION' "
        "AND ST_DWithin(sa.geometry::geography, s.coordinate, 1500)",
        56,
        "int",
    ),
    Case(
        20,
        "multi_table",
        "hard",
        "On hours when the paired weather station measured wind below 2 m/s, what "
        "was the average NO2 at roadside stations minus general stations in 2025?",
        "SELECT round((avg(a.no2) FILTER (WHERE s.station_type = 'ROADSIDE') "
        "            - avg(a.no2) FILTER (WHERE s.station_type = 'GENERAL'))::numeric, 2) "
        "FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "JOIN weather_station ws ON ws.station_code = 'wx_' || s.station_code "
        "JOIN hourly_station_weather w ON w.station_id = ws.id AND w.datetime = a.datetime "
        "WHERE a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01' "
        "AND w.wind_speed < 2",
        31.18,
        "float",
        0.2,
        holdout=True,
    ),
)
