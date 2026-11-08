"""Ingest hourly temperature, humidity and pressure for the Hong Kong stations.

Source: Open-Meteo historical archive (ERA5 reanalysis), one request per station
per month, timezone Asia/Hong_Kong.

Wind is deliberately **not** taken from here: HKO observations do that job in
`ingest.weather_hko`. The two scripts write to the same rows but own disjoint
columns, so running them in either order converges.

The HKO Open Data API was checked first (see scripts/probe_sources.py). It serves
real-time readings and daily climate summaries; its only hourly dataset is
`HHOT`, astronomical tides. It does not publish hourly temperature/humidity
history, so ERA5 fills these three columns. This is a documented deviation from
the paper, which used HKO station observations.

    uv run python -m ingest.weather --start 2025-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime

from sqlalchemy import text

from domains.hk.db import make_admin_engine
from domains.hk.stations import STATIONS, weather_code
from ingest.common import day_chunks, fetch, insert_rows, load_env, log

ARCHIVE_URL = (
    "https://archive-api.open-meteo.com/v1/archive"
    "?latitude={lat}&longitude={lon}"
    "&start_date={start}&end_date={end}"
    "&hourly=temperature_2m,relative_humidity_2m,surface_pressure"
    "&timezone=Asia%2FHong_Kong"
)

WEATHER_COLUMNS = [
    "station_id",
    "datetime",
    "temperature",
    "visibility",
    "pressure",
    "humidity",
]

WEATHER_UPSERT = """
ON CONFLICT (station_id, datetime) DO UPDATE SET
    temperature = COALESCE(EXCLUDED.temperature, hourly_station_weather.temperature),
    visibility  = COALESCE(EXCLUDED.visibility, hourly_station_weather.visibility),
    pressure    = COALESCE(EXCLUDED.pressure, hourly_station_weather.pressure),
    humidity    = COALESCE(EXCLUDED.humidity, hourly_station_weather.humidity)
"""


def ensure_weather_stations(raw) -> dict[str, int]:
    rows = [
        (
            weather_code(station),
            f"{station.name_en} ({station.hko_name})",
            station.lon,
            station.lat,
            "AUTOMATIC_WEATHER_STATION",
            station.altitude,
        )
        for station in STATIONS
    ]
    insert_rows(
        raw,
        "weather_station",
        ["station_code", "name_en", "coordinate", "station_type", "altitude"],
        rows,
        on_conflict=(
            "ON CONFLICT (station_code) DO UPDATE SET "
            "name_en = EXCLUDED.name_en, coordinate = EXCLUDED.coordinate, "
            "station_type = EXCLUDED.station_type, altitude = EXCLUDED.altitude"
        ),
        template="(%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
    )
    with raw.cursor() as cursor:
        cursor.execute("SELECT station_code, id FROM weather_station")
        ids = dict(cursor.fetchall())
    log(f"  weather stations: {len(ids)} rows")
    return ids


def ingest(raw, ids: dict[str, int], start: date, end: date, refresh: bool) -> int:
    total = 0
    for station in STATIONS:
        code = weather_code(station)
        station_total = 0
        for window_start, window_end in day_chunks(start, end, size=31):
            url = ARCHIVE_URL.format(
                lat=station.lat,
                lon=station.lon,
                start=window_start.isoformat(),
                end=window_end.isoformat(),
            )
            filename = f"era5_{station.code}_{window_start:%Y%m%d}_{window_end:%Y%m%d}.json"
            payload = fetch(url, filename=filename, refresh=refresh)
            data = json.loads(payload)
            hourly = data.get("hourly") or {}
            times = hourly.get("time") or []

            def series(key: str) -> list:
                return hourly.get(key) or [None] * len(times)

            temperature = series("temperature_2m")
            humidity = series("relative_humidity_2m")
            pressure = series("surface_pressure")

            rows = []
            for index, stamp in enumerate(times):
                moment = datetime.fromisoformat(stamp)
                if not (start <= moment.date() <= end):
                    continue
                rows.append(
                    (
                        ids[code],
                        moment,
                        temperature[index],
                        None,  # visibility is not in the archive
                        pressure[index],
                        humidity[index],
                    )
                )
            station_total += insert_rows(
                raw, "hourly_station_weather", WEATHER_COLUMNS, rows, on_conflict=WEATHER_UPSERT
            )
            time.sleep(0.2)
        raw.commit()
        total += station_total
        log(f"  {station.code:<12} {station_total:,} weather rows")
    return total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    load_env()
    engine = make_admin_engine()
    raw = engine.raw_connection()
    try:
        log(f"weather {start} .. {end}")
        ids = ensure_weather_stations(raw)
        raw.commit()
        ingested = ingest(raw, ids, start, end, args.refresh)
        log(f"  sent {ingested:,} rows")
    finally:
        raw.close()

    with engine.connect() as conn:
        summary = conn.execute(
            text(
                "SELECT count(*), min(datetime), max(datetime), count(temperature), "
                "count(wind_direction) FROM hourly_station_weather"
            )
        ).one()
    engine.dispose()
    log(f"table: {summary[0]:,} rows, {summary[1]} .. {summary[2]}, "
        f"temp={summary[3]:,}, wind_dir={summary[4]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
