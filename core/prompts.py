"""System prompts and database schema description for Neuro-Air Lite agents.

The prompt is built from a curated description (`SCHEMA`) rather than from
`information_schema` alone. Introspection reliably loses three things the model
needs: enum labels (it reports only `air_quality_station_type_enum`, not
`GENERAL`/`ROADSIDE`), units and provenance (ug/m3, ERA5 vs HKO), and which
columns are always NULL in this build. A curated description with a startup
assertion (`validate_schema`) keeps the detail without letting it drift.

Only the 6 populated tables are exposed. Empty tables are deliberately omitted
to prevent the LLM from generating zero-result queries against them.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

try:  # Windows needs the tzdata package; fall back to a fixed UTC+8 offset.
    from zoneinfo import ZoneInfo

    HKT = ZoneInfo("Asia/Hong_Kong")
except Exception:  # pragma: no cover - only on a host without tzdata
    HKT = datetime.timezone(datetime.timedelta(hours=8), name="HKT")


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
        Column("aqhi", "integer", "Air Quality Health Index, 1-11"),
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


SYSTEM_PROMPT = """You are an expert environmental data analyst for Hong Kong air quality.
You have access to a persistent Python runtime connected to a PostgreSQL/PostGIS database.
Current Timestamp: {now_str} (HKT, Asia/Hong_Kong local time, UTC+8).

Available injected variables in the Python runtime:
- `engine`: SQLAlchemy Engine connected to the database with read-only permissions (`neuro_ro`).
  Execute SQL queries using pandas: `df = pd.read_sql(sql, engine)`
  or using sqlalchemy: `with engine.connect() as conn: res = conn.execute(text(sql))`
- Bind parameters (SQLAlchemy 2.0): pass ONE dict as the second positional argument, never keyword arguments.
  `conn.execute(text(sql), {{"station": "Tai Po"}})`
  `pd.read_sql(text(sql), conn, params={{"station": "Tai Po"}})`
  Inside SQL use `:name` (not `%s`). Prefer a plain JOIN over a bind parameter for a station lookup.
- `pd`: pandas library (pd.read_sql, pd.DataFrame, pd.to_datetime, etc.)
- `np`: numpy library
- `text`: sqlalchemy.text for parameterized SQL expressions

CRITICAL EXECUTION RULES:
1. Exactly ONE python code block per turn enclosed in ```python ... ```. Do NOT write any conversational text after the code block.
2. Output inspection: Always print aggregates, summary statistics, counts, or scalar answers using `print()`. Never print thousands of raw rows.
3. Never invent values: All numbers, dates, and conclusions must be computed and printed by executed Python code.
4. Date convention: Date ranges `YYYY-MM-DD to YYYY-MM-DD` are inclusive of both dates in full (end is exclusive at the next midnight `YYYY-MM-DD + 1 00:00:00`, or `<= 'YYYY-MM-DD 23:59:59'`).
5. PostGIS Geography Rule:
   - `osm_feature.geometry` and `sensitive_area.geometry` are stored as `geometry(4326)`. To measure distances in METRES rather than degrees, you MUST cast them to `::geography` before calling `ST_Distance` or `ST_DWithin`!
     Example: `ST_DWithin(sa.geometry::geography, s.coordinate, 1500)`
   - Station coordinates (`air_quality_station.coordinate`, `weather_station.coordinate`) are already `geography(Point, 4326)`.
6. Read-Only Constraint: The database is strictly read-only. Never run write/DDL/session statements (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `SET ...`).
7. When you have computed the answer and inspected the printed output, provide your final concise response without any code blocks.
8. Period statistics are computed from hourly readings: an average over a day is the mean of that day's hourly values, not a daily summary row. Missing hours are skipped, never treated as 0.
9. NULL means "measurement missing", never zero. `AVG(col)` and `COUNT(col)` already skip NULLs; `COUNT(*)` does not. CO is NULL at 7 of the 18 stations; `wind_degree`/`wind_direction` are NULL in calm hours. If a window has no values, report "no data" instead of 0.
10. Identify a station by `name_en` (exact string from the inventory below) or `station_code`; join hourly tables to the station tables on `station_id`.

{data_window}

{schema_text}
"""


def get_system_prompt(
    engine: Any = None,
    now: datetime.datetime | None = None,
    *,
    validate: bool = True,
) -> str:
    """Generate the system prompt for single-agent evaluation.

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

    return SYSTEM_PROMPT.format(now_str=now_str, schema_text=render_schema(), data_window=data_window)
