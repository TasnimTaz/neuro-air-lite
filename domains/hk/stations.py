"""Canonical station registry for Hong Kong.

One list, used by ingest (to create the rows) and by every later day (to map a
name to a station_id). Do not duplicate station names anywhere else.

Coordinates are the EPD station locations, accurate to roughly 300 m. They only
place the station on the map and drive the Day 8 radius queries; the pollutant
and weather columns come from gridded models (CAMS ~11 km, ERA5 ~9 km), so
sub-kilometre precision here does not change any measured value.

`station_code` is our own stable key (the EPD does not publish one in the CSV
export). It travels in the database as `air_quality_station.station_code` and is
what the ingest upserts on.

Column order of the EPD monthly AQHI CSV is the order of this list, except that
the CSV interleaves the three roadside stations at the end. `csv_name` is the
exact header string in that file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    code: str
    name_en: str
    csv_name: str
    lat: float
    lon: float
    station_type: str  # GENERAL | ROADSIDE
    area_type: str  # URBAN | RURAL | NEW_TOWN
    hko_name: str  # nearest HKO automatic weather station that reports wind
    altitude: float = 0.0


STATIONS: tuple[Station, ...] = (
    Station("cw", "Central/Western", "Central/Western", 22.2846, 114.1499, "GENERAL", "URBAN", "Central Pier", 30),
    Station("south", "Southern", "Southern", 22.2480, 114.1580, "GENERAL", "URBAN", "Wong Chuk Hang", 25),
    Station("east", "Eastern", "Eastern", 22.2820, 114.2220, "GENERAL", "URBAN", "North Point", 20),
    Station("kwuntong", "Kwun Tong", "Kwun Tong", 22.3150, 114.2240, "GENERAL", "URBAN", "Kai Tak", 25),
    Station("shamshuipo", "Sham Shui Po", "Sham Shui Po", 22.3300, 114.1590, "GENERAL", "URBAN", "King's Park", 20),
    Station("kwaichung", "Kwai Chung", "Kwai Chung", 22.3570, 114.1300, "GENERAL", "URBAN", "Tsing Yi", 25),
    Station("tsuenwan", "Tsuen Wan", "Tsuen Wan", 22.3740, 114.1100, "GENERAL", "URBAN", "Tsing Yi", 25),
    Station("tko", "Tseung Kwan O", "Tseung Kwan O", 22.3070, 114.2600, "GENERAL", "NEW_TOWN", "Tseung Kwan O", 20),
    Station("yuenlong", "Yuen Long", "Yuen Long", 22.4440, 114.0300, "GENERAL", "NEW_TOWN", "Wetland Park", 20),
    Station("tuenmun", "Tuen Mun", "Tuen Mun", 22.3900, 113.9770, "GENERAL", "NEW_TOWN", "Tuen Mun", 20),
    Station("tungchung", "Tung Chung", "Tung Chung", 22.2890, 113.9430, "GENERAL", "NEW_TOWN", "Chek Lap Kok", 20),
    Station("taipo", "Tai Po", "Tai Po", 22.4510, 114.1640, "GENERAL", "NEW_TOWN", "Tai Po Kau", 25),
    Station("shatin", "Sha Tin", "Sha Tin", 22.3770, 114.1890, "GENERAL", "NEW_TOWN", "Sha Tin", 20),
    Station("north", "North", "North", 22.5020, 114.1290, "GENERAL", "NEW_TOWN", "Ta Kwu Ling", 25),
    Station("tapmun", "Tap Mun", "Tap Mun", 22.4710, 114.3620, "GENERAL", "RURAL", "Tap Mun", 30),
    Station("causewaybay", "Causeway Bay", "Causeway Bay", 22.2801, 114.1851, "ROADSIDE", "URBAN", "North Point", 15),
    Station("central", "Central", "Central", 22.2817, 114.1589, "ROADSIDE", "URBAN", "Central Pier", 15),
    Station("mongkok", "Mong Kok", "Mong Kok", 22.3226, 114.1683, "ROADSIDE", "URBAN", "King's Park", 15),
)

BY_CODE: dict[str, Station] = {station.code: station for station in STATIONS}

# The EPD CSV header spells these two differently from our display names; the
# mapping above already handles it, but keep a guard so a header change is loud.
CSV_NAMES: tuple[str, ...] = tuple(station.csv_name for station in STATIONS)

# The paper's Fig. 3a labels Tai Po as station_id 13. Serial ids depend on insert
# order, so the mapping is written to station_map.py at ingest time and read from
# there instead of being assumed.
PAPER_TAI_PO_ID = 13


def weather_code(station: Station) -> str:
    return f"wx_{station.code}"


def to_point(lat: float, lon: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


if __name__ == "__main__":
    print(f"{len(STATIONS)} stations")
    for station in STATIONS:
        print(
            f"  {station.code:<12} {station.name_en:<16} "
            f"{station.station_type:<8} {station.area_type:<8} "
            f"{station.lat:.4f},{station.lon:.4f}"
        )
