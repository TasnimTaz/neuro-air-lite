"""Ingest hourly wind for the Hong Kong stations from HKO observations.

Source: HKO "Regional weather in Hong Kong - the latest 10-minute mean wind
direction and wind speed and maximum gust" CSV, recovered from the data.gov.hk
historical archive (one ZIP per month, about 4,300 ten-minute snapshots, ~3.5 MB).

    https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_10min_wind.csv

Each ten-minute file reports every automatic weather station at once, so a month is
one download. The ten-minute readings are folded into hours: mean speed, maximum
gust, and a vector-mean direction binned to the schema's 8-point enum. HKO publishes
compass points rather than degrees, so `wind_degree` is the centre of the reported
point (N=0, NE=45, ...); it is derived, not measured.

Only the wind columns are written. `ingest.weather` fills temperature, humidity and
pressure from ERA5 in the same rows; the two passes own disjoint columns.

    uv run python -m ingest.weather_hko --start 2025-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import zipfile
from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import text

from domains.hk.db import make_admin_engine
from domains.hk.stations import STATIONS, weather_code
from ingest.common import fetch, insert_rows, load_env, log, month_keys
from ingest.weather import ensure_weather_stations

ARCHIVE = "https://app.data.gov.hk/v1/historical-archive"
WIND_URL = (
    "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_10min_wind.csv"
)

WIND_COLUMNS = ["station_id", "datetime", "wind_degree", "wind_speed", "wind_gust", "wind_direction"]

WIND_UPSERT = """
ON CONFLICT (station_id, datetime) DO UPDATE SET
    wind_degree    = EXCLUDED.wind_degree,
    wind_speed     = EXCLUDED.wind_speed,
    wind_gust      = EXCLUDED.wind_gust,
    wind_direction = EXCLUDED.wind_direction
"""

COMPASS = {
    "north": 0.0, "nne": 22.5, "ne": 45.0, "ene": 67.5,
    "east": 90.0, "ese": 112.5, "se": 135.0, "sse": 157.5,
    "south": 180.0, "ssw": 202.5, "sw": 225.0, "wsw": 247.5,
    "west": 270.0, "wnw": 292.5, "nw": 315.0, "nnw": 337.5,
}

# The feed occasionally emits a corrupted row; anything not on this list is dropped.
HKO_STATIONS = {
    "Central Pier", "Chek Lap Kok", "Cheung Chau", "Cheung Chau Beach", "Green Island",
    "Hong Kong Sea School", "Kai Tak", "King's Park", "Lamma Island", "Lau Fau Shan",
    "Ngong Ping", "North Point", "Peng Chau", "Sai Kung", "Sha Chau", "Sha Tin",
    "Shek Kong", "Stanley", "Star Ferry", "Ta Kwu Ling", "Tai Mei Tuk", "Tai Po Kau",
    "Tap Mun", "Tate's Cairn", "Tseung Kwan O", "Tsing Yi", "Tuen Mun", "Waglan Island",
    "Wetland Park", "Wong Chuk Hang",
}

SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def sector(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    return SECTORS[int(((degrees % 360) + 22.5) // 45) % 8]


def days_in_month(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return first, next_month - timedelta(days=1)


def list_versions(first: date, last: date) -> dict:
    url = (
        f"{ARCHIVE}/list-file-versions?url={quote(WIND_URL, safe='')}"
        f"&start={first:%Y%m%d}&end={last:%Y%m%d}"
    )
    return json.loads(fetch(url, filename=f"wind_versions_{first:%Y%m}.json", timeout=180))


def get_file(stamp: str, filename: str, refresh: bool) -> bytes:
    url = f"{ARCHIVE}/get-file?url={quote(WIND_URL, safe='')}&time={stamp}"
    return fetch(url, filename=filename, refresh=refresh, timeout=900)


def collect_month(bundles: dict, archive: zipfile.ZipFile, start: date, end: date) -> None:
    """Fold one month of ten-minute snapshots into per-hour accumulators."""
    for name in sorted(archive.namelist()):
        if not name.endswith(".csv"):
            continue
        text_block = archive.read(name).decode("utf-8-sig", "replace")
        for line in text_block.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 5:
                continue
            stamp, station, direction, speed, gust = parts[0], parts[1], parts[2], parts[3], parts[4]
            if station not in HKO_STATIONS:
                continue
            try:
                moment = datetime.strptime(stamp, "%Y%m%d%H%M")
            except ValueError:
                continue
            hour = moment.replace(minute=0, second=0)
            if not (start <= hour.date() <= end):
                continue

            bucket = bundles.setdefault(
                (station, hour),
                {"speed": 0.0, "n": 0, "gust": 0.0, "sin": 0.0, "cos": 0.0, "dirs": 0},
            )
            try:
                bucket["speed"] += float(speed)
                bucket["gust"] = max(bucket["gust"], float(gust))
                bucket["n"] += 1
            except ValueError:
                pass  # "Calm" carries 0, but be defensive
            degrees = COMPASS.get(direction.strip().lower())
            if degrees is not None:
                radians = math.radians(degrees)
                bucket["sin"] += math.sin(radians)
                bucket["cos"] += math.cos(radians)
                bucket["dirs"] += 1


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
    total = 0
    try:
        ids = ensure_weather_stations(raw)
        raw.commit()
        by_code = {station.code: ids[weather_code(station)] for station in STATIONS}

        for year, month in month_keys(start, end):
            first, last = days_in_month(year, month)
            metadata = list_versions(first, last)
            bundle = (metadata.get("data-files") or [{}])[0]
            if bundle.get("period") != "M":
                log(f"  {year}-{month:02d}: no monthly bundle yet, skipped")
                continue

            payload = get_file(bundle["timestamp"], f"wind_10min_{year}{month:02d}.zip", args.refresh)
            archive = zipfile.ZipFile(io.BytesIO(payload))
            buckets: dict[tuple[str, datetime], dict] = {}
            collect_month(buckets, archive, first, last)

            rows = []
            codes_by_hko: dict[str, list[str]] = {}
            for station in STATIONS:
                codes_by_hko.setdefault(station.hko_name, []).append(station.code)
            for (hko_name, hour), bucket in buckets.items():
                if bucket["n"] == 0:
                    continue
                speed = bucket["speed"] / bucket["n"] / 3.6  # km/h -> m/s
                gust = bucket["gust"] / 3.6
                if bucket["dirs"]:
                    degrees = math.degrees(math.atan2(bucket["sin"], bucket["cos"])) % 360
                else:
                    degrees = None
                for code in codes_by_hko.get(hko_name, ()):
                    rows.append((
                        by_code[code], hour, degrees, round(speed, 3),
                        round(gust, 3), sector(degrees),
                    ))

            sent = insert_rows(raw, "hourly_station_weather", WIND_COLUMNS, rows, on_conflict=WIND_UPSERT)
            raw.commit()
            total += sent
            log(f"  {year}-{month:02d}: {sent:,} wind rows")
    finally:
        raw.close()

    with engine.connect() as conn:
        summary = conn.execute(
            text(
                "SELECT count(*), min(datetime), max(datetime), count(wind_speed), "
                "count(wind_direction) FROM hourly_station_weather WHERE wind_speed IS NOT NULL"
            )
        ).one()
    engine.dispose()
    log(f"wind: {summary[0]:,} rows, {summary[1]} .. {summary[2]}, "
        f"speed={summary[3]:,}, dir={summary[4]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
