"""Create or refresh the read-only joined views used by the demo.

The air-quality and weather tables share a ``station_id`` column name but the
ids are different sequences.  The weather station is paired to an air-quality
station by the ``wx_<station_code>`` prefix, so a naive
``air_quality_station.id = hourly_station_weather.station_id`` join silently
returns no rows.  ``paired_hourly_weather`` makes the correct pairing an
explicit, read-only relation instead of relying on prose alone.

This script is idempotent and changes no data.  It uses the admin engine only
because creating a view and granting a privilege are DDL operations; the agent
never receives this engine.

Usage::

    uv run python scripts/migrate_views.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from core.env import admin_url, load_admin_env  # noqa: E402
from domains.hk.db import make_admin_engine  # noqa: E402

VIEW_SQL = """
CREATE OR REPLACE VIEW paired_hourly_weather AS
SELECT
    a.id AS aq_station_id,
    a.station_code AS aq_station_code,
    a.name_en AS aq_station_name,
    w.datetime,
    w.temperature,
    w.humidity,
    w.pressure,
    w.wind_speed,
    w.wind_gust,
    w.wind_degree,
    w.wind_direction
FROM air_quality_station a
JOIN weather_station ws
  ON ws.station_code = 'wx_' || a.station_code
JOIN hourly_station_weather w
  ON w.station_id = ws.id
"""

GRANT_SQL = "GRANT SELECT ON paired_hourly_weather TO neuro_ro"

CHECK_SQL = """
SELECT count(*) AS rows,
       count(*) FILTER (WHERE aq_station_code = 'taipo') AS taipo_rows
FROM paired_hourly_weather
"""


def main() -> int:
    load_admin_env(override=True)
    engine = make_admin_engine(admin_url())
    try:
        with engine.begin() as conn:
            conn.execute(text(VIEW_SQL))
            conn.execute(text(GRANT_SQL))
            rows, taipo_rows = conn.execute(text(CHECK_SQL)).one()
        print(
            "paired_hourly_weather: "
            f"created/replaced, GRANT SELECT TO neuro_ro, "
            f"{rows:,} rows total, {taipo_rows:,} Tai Po rows"
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
