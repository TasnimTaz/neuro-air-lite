"""Ingest hourly air quality for the Hong Kong stations.

Both columns of `hourly_station_air_quality` come from the EPD, so the table holds
real observations end to end:

  AQHI          EPD monthly CSV export
                https://www.aqhi.gov.hk/epd/ddata/html/history/<year>/<yyyymm>_Eng.csv
  pollutants    EPD "Past 24-hour Pollutant Concentration" XML, recovered from the
                data.gov.hk Historical Archive
                https://app.data.gov.hk/v1/historical-archive

Why the archive: the live XML only holds the last 24 hours, and the EPIC historical
portal is a JSF form guarded by a captcha. data.gov.hk keeps a copy of every hourly
XML the feed ever produced, and also bundles each month into one ZIP (about 720
snapshots, ~7 MB), which is a single request per month.

Units: the published data dictionary
(`get-data-dictionary?...&date=20240418`) states "The concentration unit is ug/m3"
for every pollutant, CO included. CO is *not* in mg/m3 here: the feed reports
roughly 500-900, which is 0.5-0.9 mg/m3, the normal Hong Kong range. No scaling.

    uv run python -m ingest.aq --start 2025-01-01 --end 2025-12-31
    uv run python -m ingest.aq --aqhi-only
    uv run python -m ingest.aq --pollutants-only --refresh
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from sqlalchemy import text

from domains.hk.db import make_admin_engine
from domains.hk.stations import CSV_NAMES, STATIONS
from ingest.common import fetch, insert_rows, load_env, log, month_keys

AQHI_URL = "https://www.aqhi.gov.hk/epd/ddata/html/history/{year}/{year}{month:02d}_Eng.csv"

ARCHIVE = "https://app.data.gov.hk/v1/historical-archive"
PC_URL = "https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml"

AQ_COLUMNS = [
    "station_id",
    "datetime",
    "aqhi",
    "aqhi_level",
    "pm2_5",
    "pm10",
    "o3",
    "so2",
    "no2",
    "co",
]

# The two passes own different columns, so neither can blank out the other's data.
AQHI_UPSERT = """
ON CONFLICT (station_id, datetime) DO UPDATE SET
    aqhi       = EXCLUDED.aqhi,
    aqhi_level = EXCLUDED.aqhi_level
"""

# Pollutant values are overwritten rather than coalesced: the EPD feed is the only
# authority for these columns, and "-" means the station does not measure it.
POLLUTANT_UPSERT = """
ON CONFLICT (station_id, datetime) DO UPDATE SET
    pm2_5 = EXCLUDED.pm2_5,
    pm10  = EXCLUDED.pm10,
    o3    = EXCLUDED.o3,
    so2   = EXCLUDED.so2,
    no2   = EXCLUDED.no2,
    co    = EXCLUDED.co
"""

NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
RECORD = re.compile(
    r"<PollutantConcentration>\s*<StationName>(.*?)</StationName>\s*"
    r"<DateTime>(.*?)</DateTime>\s*"
    r"<NO2>(.*?)</NO2>\s*<O3>(.*?)</O3>\s*<SO2>(.*?)</SO2>\s*<CO>(.*?)</CO>\s*"
    r"<PM10>(.*?)</PM10>\s*<PM2.5>(.*?)</PM2.5>\s*</PollutantConcentration>",
    re.S,
)


def aqhi_level(value: int | None) -> str | None:
    if value is None:
        return None
    if value <= 3:
        return "LOW"
    if value <= 6:
        return "MODERATE"
    if value == 7:
        return "HIGH"
    if value <= 10:
        return "VERY HIGH"
    return "SERIOUS"


def parse_number(cell: str) -> float | None:
    """EPD writes '-' when a station does not measure a pollutant, and '3*' for
    values adopted from a neighbouring station."""
    cell = (cell or "").strip()
    if cell in {"", "-"}:
        return None
    match = NUMBER.search(cell)
    return float(match.group(0)) if match else None


def parse_aqhi(cell: str) -> int | None:
    """Parse an AQHI cell, encoding EPD's top band '10+' as 11.

    The published index runs 1-10 and '10+'; `aqhi_level` maps >10 to SERIOUS.
    `parse_number('10+')` would silently return 10 and relabel every serious
    hour as VERY HIGH, which is exactly what happened in the first ingest.
    """
    cell = (cell or "").strip()
    if cell.startswith("10+"):
        return 11
    value = parse_number(cell)
    return int(value) if value is not None else None


def ensure_stations(raw) -> dict[str, int]:
    rows = [
        (s.code, s.name_en, s.lon, s.lat, s.station_type, s.area_type, s.altitude)
        for s in STATIONS
    ]
    insert_rows(
        raw,
        "air_quality_station",
        ["station_code", "name_en", "coordinate", "station_type", "area_type", "altitude"],
        rows,
        on_conflict=(
            "ON CONFLICT (station_code) DO UPDATE SET "
            "name_en = EXCLUDED.name_en, coordinate = EXCLUDED.coordinate, "
            "station_type = EXCLUDED.station_type, area_type = EXCLUDED.area_type, "
            "altitude = EXCLUDED.altitude"
        ),
        template="(%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s)",
    )
    with raw.cursor() as cursor:
        cursor.execute("SELECT station_code, id FROM air_quality_station")
        ids = dict(cursor.fetchall())
    log(f"  stations: {len(ids)} rows")
    return ids


def write_station_map(ids: dict[str, int]) -> None:
    """Persist the serial -> code mapping so later days never guess an id."""
    from domains.hk import station_map

    path = station_map.__file__
    lines = [
        '"""Generated by `python -m ingest.aq`. Do not edit by hand.',
        "",
        "Maps the database's serial station ids to the canonical registry codes.",
        "The paper's Fig. 3a calls Tai Po station_id 13; ours is whatever the",
        'insert order produced, so always look it up here.',
        '"""',
        "",
        "STATION_ID: dict[str, int] = {",
    ]
    for code in sorted(ids):
        lines.append(f'    "{code}": {ids[code]},')
    lines += [
        "}",
        "",
        "CODE: dict[int, str] = {value: key for key, value in STATION_ID.items()}",
        "",
        'TAI_PO = STATION_ID["taipo"]',
        "",
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    log(f"  station map written to {path}")


# --------------------------------------------------------------------------- AQHI


def ingest_aqhi(raw, ids: dict[str, int], start: date, end: date, refresh: bool) -> int:
    total = 0
    for year, month in month_keys(start, end):
        url = AQHI_URL.format(year=year, month=month)
        payload = fetch(url, filename=f"aqhi_{year}{month:02d}_Eng.csv", refresh=refresh)
        lines = payload.decode("utf-8-sig").splitlines()

        header_at = next(
            (i for i, line in enumerate(lines) if line.startswith("Date,Hour,")), None
        )
        if header_at is None:
            raise RuntimeError(f"no header row in {url}")

        header = [cell.strip() for cell in lines[header_at].split(",")]
        columns = header[2:]
        unknown = [name for name in columns if name not in CSV_NAMES]
        if unknown:
            raise RuntimeError(f"unexpected station names in {url}: {unknown}")
        by_csv_name = {station.csv_name: station for station in STATIONS}

        rows = []
        current: date | None = None
        for line in lines[header_at + 1 :]:
            cells = [cell.strip() for cell in line.split(",")]
            if len(cells) < 3:
                continue
            if cells[1].startswith("Daily"):
                # A "Daily Max" summary row closes every day; it is not an hour.
                continue
            if cells[0]:
                try:
                    current = datetime.strptime(cells[0], "%Y-%m-%d").date()
                except ValueError:
                    continue
            if current is None:
                continue
            # "Hour" runs 01..24 and labels the hour, so 01 is 00:00 local time.
            stamp = datetime.combine(current, datetime.min.time()) + timedelta(hours=int(cells[1]) - 1)
            if not (start <= stamp.date() <= end):
                continue
            for name, cell in zip(columns, cells[2:]):
                aqhi = parse_aqhi(cell)
                if aqhi is None:
                    continue
                rows.append((ids[by_csv_name[name].code], stamp, aqhi, aqhi_level(aqhi),
                             None, None, None, None, None, None))

        sent = insert_rows(raw, "hourly_station_air_quality", AQ_COLUMNS, rows, on_conflict=AQHI_UPSERT)
        raw.commit()
        total += sent
        log(f"  {year}-{month:02d}: {sent:,} AQHI rows")
    return total


# --------------------------------------------------------------------- pollutants


def _archive_query() -> str:
    return f"url={quote(PC_URL, safe='')}"


def list_versions(month_start: date, month_end: date) -> dict:
    url = (
        f"{ARCHIVE}/list-file-versions?{_archive_query()}"
        f"&start={month_start:%Y%m%d}&end={month_end:%Y%m%d}"
    )
    payload = fetch(url, filename=f"archive_versions_{month_start:%Y%m}.json", timeout=180)
    return json.loads(payload)


def get_file(stamp: str, filename: str, refresh: bool) -> bytes:
    url = f"{ARCHIVE}/get-file?{_archive_query()}&time={stamp}"
    return fetch(url, filename=filename, refresh=refresh, timeout=900)


def parse_snapshot(text: str, into: dict[tuple[str, datetime], tuple]) -> None:
    """Merge one XML snapshot into `into`; a later snapshot wins for the same hour."""
    for match in RECORD.finditer(text):
        station, when, no2, o3, so2, co, pm10, pm2_5 = match.groups()
        moment = parsedate_to_datetime(when.strip())
        # The feed timestamps are already +0800 wall-clock time; drop the offset so
        # the column stays a naive HK-local timestamp like the rest of the schema.
        moment = moment.replace(tzinfo=None)
        into[(station.strip(), moment)] = (
            parse_number(no2),
            parse_number(o3),
            parse_number(so2),
            parse_number(co),
            parse_number(pm10),
            parse_number(pm2_5),
        )


def days_in_month(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return first, next_month - timedelta(days=1)


def ingest_pollutants(raw, ids: dict[str, int], start: date, end: date, refresh: bool) -> int:
    by_name = {station.csv_name: station for station in STATIONS}
    total = 0

    for year, month in month_keys(start, end):
        first, last = days_in_month(year, month)
        metadata = list_versions(first, last)

        # The monthly bundle is one request for ~720 snapshots. Months that are not
        # finished yet have no bundle, so those fall back to one snapshot per day.
        bundle = (metadata.get("data-files") or [{}])[0]
        values: dict[tuple[str, datetime], tuple] = {}

        if bundle.get("period") == "M":
            payload = get_file(
                bundle["timestamp"], f"epd_24pc_{year}{month:02d}.zip", refresh
            )
            archive = zipfile.ZipFile(io.BytesIO(payload))
            names = sorted(n for n in archive.namelist() if n.endswith(".xml"))
            log(f"  {year}-{month:02d}: bundle of {len(names)} snapshots")
            for name in names:
                parse_snapshot(archive.read(name).decode("utf-8", "replace"), values)
        else:
            stamps = metadata.get("timestamps") or []
            if not stamps:
                log(f"  {year}-{month:02d}: no archive versions, skipped")
                continue
            # Keep the last snapshot of each day: it carries that day's 24 hours.
            latest_per_day: dict[str, str] = {}
            for stamp in stamps:
                latest_per_day[stamp[:8]] = stamp
            log(f"  {year}-{month:02d}: {len(latest_per_day)} daily snapshots")
            for day, stamp in sorted(latest_per_day.items()):
                payload = get_file(stamp, f"epd_24pc_{stamp}.xml", refresh)
                parse_snapshot(payload.decode("utf-8", "replace"), values)

        rows = []
        for (station_name, moment), (no2, o3, so2, co, pm10, pm2_5) in values.items():
            if not (start <= moment.date() <= end):
                continue
            station = by_name.get(station_name)
            if station is None:
                continue
            rows.append((ids[station.code], moment, None, None, pm2_5, pm10, o3, so2, no2, co))

        sent = insert_rows(
            raw, "hourly_station_air_quality", AQ_COLUMNS, rows, on_conflict=POLLUTANT_UPSERT
        )
        raw.commit()
        total += sent
        log(f"  {year}-{month:02d}: {sent:,} pollutant rows")
    return total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--refresh", action="store_true", help="ignore the download cache")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--aqhi-only", action="store_true")
    mode.add_argument("--pollutants-only", action="store_true")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        print("--end is before --start", file=sys.stderr)
        return 2

    load_env()
    engine = make_admin_engine()
    raw = engine.raw_connection()
    try:
        log(f"air quality {start} .. {end}")
        ids = ensure_stations(raw)
        write_station_map(ids)
        raw.commit()

        if not args.pollutants_only:
            log("AQHI from EPD monthly CSV")
            ingest_aqhi(raw, ids, start, end, args.refresh)
        if not args.aqhi_only:
            log("pollutants from the data.gov.hk historical archive")
            ingest_pollutants(raw, ids, start, end, args.refresh)
    finally:
        raw.close()

    with engine.connect() as conn:
        summary = conn.execute(
            text(
                "SELECT count(*), min(datetime), max(datetime), "
                "count(aqhi), count(pm2_5), count(no2) FROM hourly_station_air_quality"
            )
        ).one()
    engine.dispose()
    log(f"table: {summary[0]:,} rows, {summary[1]} .. {summary[2]}, "
        f"aqhi={summary[3]:,}, pm2_5={summary[4]:,}, no2={summary[5]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
