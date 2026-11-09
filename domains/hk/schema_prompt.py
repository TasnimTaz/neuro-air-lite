"""Hong Kong domain prompt: curated schema, knowledge base and worked examples.

The schema description is curated rather than introspected. Introspection reports
only enum *type* names (e.g. `air_quality_station_type_enum`), not labels
(`GENERAL`/`ROADSIDE`), and loses units and provenance (ERA5 vs HKO). A curated
description with a startup assertion (`validate_schema`) keeps the detail without
letting it drift.

Only the 6 populated tables are exposed. Empty tables are deliberately omitted:
a query against an empty table returns a silent 0, which is harder to diagnose
than a missing-table error.

Knowledge-base sources (accessed 2026-11-09):

* EPD Air Quality Objectives (current, effective 2025-04-11)
  https://www.epd.gov.hk/epd/english/environmentinhk/air/air_quality_objectives/air_quality_objectives.html
* EPD Historical Air Quality Objectives (2022-01-01 .. 2025-04-10)
  https://www.epd.gov.hk/epd/sites/default/files/epd/english/environmentinhk/air/air_quality_objectives/files/Historical_AQOs_Eng.pdf
* AQHI FAQ (bands, 1-10 and 10+)
  https://www.aqhi.gov.hk/en/what-is-aqhi/faqs.html
* WHO global air quality guidelines (2021)
  https://www.who.int/publications/i/item/9789240034228

The prompt is assembled by concatenation, never `str.format`, so JSON braces in
the examples are literal and cannot raise `KeyError`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from core.prompts import COMMON_INSTRUCTIONS, HKT, assemble


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    note: str = ""
    nullable: bool = True


SCHEMA: dict[str, tuple[Column, ...]] = {
    "air_quality_station": (
        Column("id", "integer", "PRIMARY KEY", False),
        Column("station_code", "varchar(20)", "UNIQUE natural key, e.g. 'taipo', 'cw', 'central'"),
        Column("name_en", "varchar(200)", "display name, e.g. 'Tai Po', 'Central', 'Central/Western'"),
        Column("coordinate", "geography(Point, 4326)", "WGS84 degrees; distance arithmetic returns metres"),
        Column("altitude", "double precision", "metres"),
        Column("station_type", "air_quality_station_type_enum", "'GENERAL' | 'ROADSIDE'"),
        Column("area_type", "air_quality_station_area_type_enum", "'URBAN' | 'RURAL' | 'NEW_TOWN'"),
    ),
    "hourly_station_air_quality": (
        Column("id", "integer", "PRIMARY KEY", False),
        Column("station_id", "integer", "REFERENCES air_quality_station.id"),
        Column("datetime", "timestamp", "Hong Kong local time, naive (no UTC offset), one row per hour"),
        Column("aqhi", "integer", "official EPD index; 1-10, and 11 encodes EPD's '10+' (SERIOUS)"),
        Column("aqhi_level", "air_quality_health_index_level_enum", "'LOW' | 'MODERATE' | 'HIGH' | 'VERY HIGH' | 'SERIOUS'"),
        Column("pm2_5", "double precision", "ug/m3"),
        Column("pm10", "double precision", "ug/m3"),
        Column("o3", "double precision", "ug/m3"),
        Column("so2", "double precision", "ug/m3"),
        Column("no2", "double precision", "ug/m3"),
        Column("co", "double precision", "ug/m3; NULL at 7 of 18 stations (not measured there)"),
    ),
    "weather_station": (
        Column("id", "integer", "PRIMARY KEY", False),
        Column("station_code", "varchar(50)", "UNIQUE, 'wx_<aq_station_code>', e.g. 'wx_taipo'"),
        Column("name_en", "varchar(200)", "HKO automatic weather station name, e.g. 'Tai Po (Tai Po Kau)'"),
        Column("coordinate", "geography(Point, 4326)", "WGS84 degrees"),
        Column("altitude", "double precision", "metres"),
        Column("station_type", "weather_station_type_enum", "'AUTOMATIC_WEATHER_STATION' | 'MANNED_WEATHER_STATION'"),
    ),
    "hourly_station_weather": (
        Column("id", "integer", "PRIMARY KEY", False),
        Column("station_id", "integer", "REFERENCES weather_station.id"),
        Column("datetime", "timestamp", "Hong Kong local time, naive, one row per hour"),
        Column("temperature", "double precision", "deg C, ERA5 reanalysis"),
        Column("humidity", "double precision", "percent, ERA5 reanalysis"),
        Column("pressure", "double precision", "hPa, ERA5 reanalysis"),
        Column("wind_speed", "double precision", "m/s, HKO 10-minute observations folded to hourly"),
        Column("wind_gust", "double precision", "m/s, maximum 10-minute gust in the hour, HKO"),
        Column("wind_degree", "double precision", "degrees, 0 = North, clockwise; NULL when calm"),
        Column("wind_direction", "wind_direction_enum", "'N' | 'NE' | 'E' | 'SE' | 'S' | 'SW' | 'W' | 'NW'; NULL when calm"),
    ),
    "osm_feature": (
        Column("id", "integer", "PRIMARY KEY", False),
        Column("name", "varchar(255)", "feature name, often bilingual"),
        Column("sub_type", "varchar(100)", "e.g. 'school', 'hospital', 'park', 'residential', 'apartments'"),
        Column("geometry", "geometry(Geometry, 4326)", "mixed geometry types; POINT rows in this build"),
        Column("geometry_type", "geometry_type_enum", "'POINT' | 'LINESTRING' | 'POLYGON' | 'MULTIPOLYGON'"),
        Column(
            "main_type",
            "osm_feature_main_type_enum",
            "'BUILDING' | 'PUBLIC' | 'RELIGIOUS' | 'LEISURE' | 'INDUSTRIAL' | 'CEMETERY' | 'WATER' | ...",
        ),
    ),
    "sensitive_area": (
        Column("id", "integer", "PRIMARY KEY", False),
        Column(
            "category",
            "sensitive_area_category_enum",
            "'EDUCATIONAL_INSTITUTION' | 'HEALTH_CARE_INSTITUTION' | 'PLACE_OF_WORSHIP' (only these three hold rows)",
            False,
        ),
        Column("name", "varchar(255)", "institution name"),
        Column("geometry", "geometry(Geometry, 4326)", "POINT; cast to ::geography for metres", False),
        Column("geometry_type", "geometry_type_enum", "'POINT'", False),
    ),
}

# Columns that exist in the database but are deliberately not advertised: they are
# either always NULL in this build or pure bookkeeping. They are listed here so
# `validate_schema` can tell "known and hidden" apart from "new, undocumented"
# column that would silently invalidate the prompt.
HIDDEN_COLUMNS: dict[str, dict[str, str]] = {
    "air_quality_station": {
        "address_en": "all NULL in this build",
        "created_at": "ingest bookkeeping",
    },
    "hourly_station_air_quality": {
        "no2_ar": "always NULL (added-health-risk columns not ingested)",
        "o3_ar": "always NULL",
        "pm_ar": "always NULL",
        "so2_ar": "always NULL",
        "pm_ar_source": "always NULL",
    },
    "weather_station": {
        "address_en": "all NULL in this build",
        "created_at": "ingest bookkeeping",
    },
    "hourly_station_weather": {
        "visibility": "always NULL (not ingested)",
    },
    "sensitive_area": {
        "area": "always NULL in this build",
    },
}

POPULATED_TABLES = list(SCHEMA)


DOMAIN_RULES = """HONG KONG DOMAIN RULES:
- Identify a station by `name_en` (exact string from the live inventory below) or `station_code`; join hourly tables on `station_id`. Never hard-code a station id.
- `air_quality_station.coordinate` and `weather_station.coordinate` are `geography(Point, 4326)`, so `ST_Distance` returns metres.
- `osm_feature.geometry` and `sensitive_area.geometry` are `geometry(Geometry, 4326)`. Cast them to `::geography` before `ST_Distance`/`ST_DWithin`, otherwise the unit is degrees. Example: `ST_DWithin(sa.geometry::geography, s.coordinate, 1500)`.
- Each `weather_station` is paired one-to-one with an `air_quality_station`: the weather `station_code` is `'wx_<air quality station_code>'` (e.g. `'wx_taipo'` for `'taipo'`). Weather holds temperature, humidity, pressure and wind; air quality holds the pollutants and AQHI.
- Period statistics come from hourly readings: a daily mean is the mean of that day's hourly values, not a daily summary row. Missing hours are skipped, never treated as 0.
- NULL means "measurement missing", never zero. `AVG(col)` and `COUNT(col)` skip NULLs; `COUNT(*)` does not. CO is NULL at 7 of the 18 stations; `wind_degree`/`wind_direction` are NULL in calm hours. If a window has no values, report "no data", not 0.
- AQHI is the official EPD index; never recompute it from the pollutant columns."""


KNOWLEDGE_BASE = """<knowledge_base>
AQHI (Air Quality Health Index, the official EPD index):
  1-3 LOW | 4-6 MODERATE | 7 HIGH | 8-10 VERY HIGH | 10+ SERIOUS.
  EPD publishes the top band as "10+"; the database encodes it as `aqhi = 11`, so
  `aqhi_level = 'SERIOUS'` is equivalent to `aqhi > 10`. `aqhi_level` is authoritative.

Hong Kong Air Quality Objectives (AQO), concentration limits in ug/m3.
The AQO changed on 2025-04-11, in the middle of the data window. For a period that
crosses that date, apply the limit in force on each date and state which one you used.
  From 2025-04-11 (current):
    PM2.5  24h 37.5 (18 exceedances allowed) | annual 15
    PM10   24h 75 (9) | annual 30
    NO2    1h 200 (18) | 24h 120 (9) | annual 40
    SO2    10min 500 (3) | 24h 40 (3)
    O3     8h 160 (9) | peak season 100
    CO     1h 30,000 | 8h 10,000 | 24h 4,000
  From 2022-01-01 to 2025-04-10 (previous):
    PM2.5  24h 50 (35) | annual 25
    PM10   24h 100 (9) | annual 50
    NO2    1h 200 (18) | annual 40
    SO2    10min 500 (3) | 24h 50 (3)
    O3     8h 160 (9)
    CO     1h 30,000 | 8h 10,000
  The "allowed exceedances" column is a compliance allowance, not a health target.

WHO 2021 global air quality guidelines (comparison only, not Hong Kong law):
  PM2.5 5 annual / 15 24h | PM10 15 / 45 | NO2 10 / 25
  O3 60 peak season / 100 8h | SO2 40 24h | CO 4 mg/m3 (= 4,000 ug/m3) 24h.

Units: every pollutant column is ug/m3, CO included. Do not multiply CO by 1000.

Sources (accessed 2026-11-09):
  EPD AQO: https://www.epd.gov.hk/epd/english/environmentinhk/air/air_quality_objectives/air_quality_objectives.html
  EPD historical AQOs: https://www.epd.gov.hk/epd/sites/default/files/epd/english/environmentinhk/air/air_quality_objectives/files/Historical_AQOs_Eng.pdf
  AQHI FAQ: https://www.aqhi.gov.hk/en/what-is-aqhi/faqs.html
  WHO 2021: https://www.who.int/publications/i/item/9789240034228
</knowledge_base>"""


EXAMPLES = """<examples>
Example 1: temporal trend plus a chart.
User: How did Tai Po's PM2.5 change during November 2025?
Assistant:
```python
sql = \"\"\"
SELECT a.datetime::date AS day, avg(a.pm2_5) AS pm25
FROM hourly_station_air_quality a
JOIN air_quality_station s ON s.id = a.station_id
WHERE s.name_en = 'Tai Po'
  AND a.datetime >= '2025-11-01' AND a.datetime < '2025-12-01'
GROUP BY 1 ORDER BY 1
\"\"\"
df = pd.read_sql(text(sql), engine)
charts.line(
    'Daily mean PM2.5 at Tai Po, November 2025',
    [str(day) for day in df['day']],
    {'PM2.5 (ug/m3)': df['pm25'].round(2).tolist()},
    'ug/m3',
)
ANSWER = round(float(df['pm25'].mean()), 2)
print('days plotted:', len(df), 'monthly mean:', ANSWER)
```

Example 2: geocode an address, then find the nearest station with a bind parameter.
User: Which air quality station is nearest to The University of Hong Kong?
Assistant:
```python
location = geocode('The University of Hong Kong')
sql = \"\"\"
SELECT s.name_en,
       ST_Distance(s.coordinate,
                   ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) / 1000.0 AS km
FROM air_quality_station s
ORDER BY km
LIMIT 1
\"\"\"
with engine.connect() as conn:
    name, km = conn.execute(
        text(sql), {'lon': location['longitude'], 'lat': location['latitude']}
    ).one()
ANSWER = round(float(km), 2)
print('nearest station:', name, 'distance_km:', ANSWER)
```

Example 3: radius count in metres (cast geometry to geography).
User: How many places of worship are within 2 km of the Yuen Long station?
Assistant:
```python
sql = \"\"\"
SELECT count(*) AS n
FROM sensitive_area sa, air_quality_station s
WHERE s.name_en = 'Yuen Long'
  AND sa.category = 'PLACE_OF_WORSHIP'
  AND ST_DWithin(sa.geometry::geography, s.coordinate, 2000)
\"\"\"
with engine.connect() as conn:
    ANSWER = conn.execute(text(sql)).scalar_one()
print('places of worship within 2 km:', ANSWER)
```

Example 4: daily aggregation against an AQO limit.
User: How many days did Yuen Long's daily mean PM2.5 exceed the current AQO in late 2025?
Assistant:
```python
sql = \"\"\"
WITH daily AS (
    SELECT a.datetime::date AS day, avg(a.pm2_5) AS pm25
    FROM hourly_station_air_quality a
    JOIN air_quality_station s ON s.id = a.station_id
    WHERE s.name_en = 'Yuen Long'
      AND a.datetime >= '2025-04-11' AND a.datetime < '2026-01-01'
    GROUP BY 1
)
SELECT count(*) FROM daily WHERE pm25 > 37.5
\"\"\"
with engine.connect() as conn:
    ANSWER = conn.execute(text(sql)).scalar_one()
print('days above the 2025-04-11 AQO (37.5 ug/m3):', ANSWER)
```
</examples>"""


def render_schema() -> str:
    lines = ["<database_schema>"]
    for table, columns in SCHEMA.items():
        lines.append(f"\nTable {table}:")
        for column in columns:
            parts = [column.type]
            if column.note:
                parts.append(column.note)
            parts.append("nullable" if column.nullable else "NOT NULL")
            lines.append(f"  - {column.name}: " + "; ".join(parts))
    lines.append("</database_schema>")
    return "\n".join(lines)


def validate_schema(engine: Any) -> None:
    """Assert the curated description still matches the live database.

    Raises instead of degrading: a prompt that names a column the database does
    not have produces a plausible-looking SQL error on every query, which is far
    harder to diagnose downstream than a failed startup.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ANY(:tables)
                """
            ),
            {"tables": list(SCHEMA)},
        ).fetchall()

    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)

    problems: list[str] = []
    for table, columns in SCHEMA.items():
        if table not in actual:
            problems.append(f"{table}: table is missing")
            continue
        declared = {column.name for column in columns}
        hidden = set(HIDDEN_COLUMNS.get(table, {}))
        absent = declared - actual[table]
        unknown = actual[table] - declared - hidden
        if absent:
            problems.append(f"{table}: prompt declares columns that do not exist: {sorted(absent)}")
        if unknown:
            problems.append(f"{table}: database has columns the prompt does not describe: {sorted(unknown)}")

    if problems:
        raise RuntimeError("schema prompt is out of sync with the database:\n  " + "\n  ".join(problems))


def data_window_text(engine: Any) -> str:
    """Describe the date range and station inventory actually present.

    The model cannot tell that the database is historical and stops in 2025
    without this, and it will happily ask for "yesterday", get no rows, and
    report 0. Kept inside try/except so a missing table degrades to no extra
    text instead of breaking the prompt.
    """
    try:
        with engine.connect() as conn:
            aq_first, aq_last = conn.execute(
                text("SELECT min(datetime), max(datetime) FROM hourly_station_air_quality")
            ).one()
            wx_first, wx_last = conn.execute(
                text("SELECT min(datetime), max(datetime) FROM hourly_station_weather")
            ).one()
            stations = conn.execute(
                text(
                    "SELECT station_code, name_en, station_type "
                    "FROM air_quality_station ORDER BY id"
                )
            ).fetchall()
    except Exception:
        return ""

    inventory = ", ".join(f"{name} [{code}] {kind}" for code, name, kind in stations)

    return (
        "Data coverage (Hong Kong local time, naive timestamps, no UTC offset):\n"
        f"- hourly_station_air_quality.datetime: {aq_first} .. {aq_last}\n"
        f"- hourly_station_weather.datetime:     {wx_first} .. {wx_last}\n"
        f"- air_quality_station ({len(stations)} stations, name_en [station_code] station_type): {inventory}\n"
        "- Each weather_station is paired one-to-one with an air_quality_station: "
        "weather station_code is 'wx_<air quality station_code>' (e.g. 'wx_taipo' for 'taipo').\n"
        "The database is historical only. A request for a date outside the range "
        "above has no rows: say so instead of reporting 0."
    )


def get_system_prompt(
    engine: Any = None,
    now: datetime.datetime | None = None,
    *,
    validate: bool = True,
) -> str:
    """Generate the single-agent system prompt for the Hong Kong domain.

    With an engine, the schema is checked against the live database and the
    actual data window and station inventory are injected.
    """
    now_dt = now or datetime.datetime.now(HKT)
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(HKT)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    if engine is not None:
        if validate:
            validate_schema(engine)
        data_window = data_window_text(engine)
    else:
        data_window = ""

    return assemble(
        COMMON_INSTRUCTIONS,
        f"Current Timestamp: {now_str} (HKT, Asia/Hong_Kong local time, UTC+8).",
        DOMAIN_RULES,
        KNOWLEDGE_BASE,
        EXAMPLES,
        data_window,
        render_schema(),
    )
