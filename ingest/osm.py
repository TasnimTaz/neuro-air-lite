"""Load OpenStreetMap features for Hong Kong.

Two sources, one table:

  pbf        Geofabrik's Hong Kong extract (~38 MB), parsed with pyosmium. This is
             the source the plan calls for: one download covers buildings, roads and
             every POI with real polygon geometry.
  overpass   Fallback for when the Geofabrik host is unusable (it throttles to a few
             KB/s from some networks). Overpass cannot serve the ~137k building
             footprints, so this path loads only the point features the later days
             actually query: schools, hospitals, places of worship, parks, industry,
             plus building centroids in the Tai Po Area of Interest (AOI).

`--source auto` uses the cached PBF when one is present and Overpass otherwise.

The Overpass path also fills `sensitive_area` for schools, hospitals and places of
worship, because Day 8 asks for "sensitive receptors near a station" and the schema
has a table dedicated to exactly that.

Idempotency: each source empties the rows it owns and refills them inside one
transaction, so a re-run converges instead of duplicating and a failure leaves the
previous contents intact.

    uv run python -m ingest.osm
    uv run python -m ingest.osm --source overpass
    uv run python -m ingest.osm --source pbf --pbf D:\\data\\hong-kong-latest.osm.pbf
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import osmium
from shapely.geometry import LineString, Point, Polygon

from domains.hk.db import make_admin_engine
from ingest.common import CACHE, fetch, insert_rows, load_env, log

PBF_URL = "https://download.geofabrik.de/asia/china/hong-kong-latest.osm.pbf"
PBF_NAME = "hong-kong-latest.osm.pbf"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

GEOMETRY_INDEX = "osm_feature_geometry_gix"

OSM_COLUMNS = ["name", "sub_type", "geometry", "geometry_type", "main_type"]
OSM_TEMPLATE = "(%s, %s, ST_GeomFromWKB(decode(%s, 'hex'), 4326), %s, %s)"

AREA_COLUMNS = ["category", "name", "geometry", "geometry_type"]
AREA_TEMPLATE = "(%s, %s, ST_GeomFromWKB(decode(%s, 'hex'), 4326), %s)"

SCHOOL_AMENITIES = {"school", "university", "college", "kindergarten"}
HOSPITAL_AMENITIES = {"hospital", "clinic", "doctors", "dentist"}
PARK_LEISURE = {"park", "garden", "playground", "nature_reserve"}

# (query body, cache slug, sensitive_area category or None, is_raw_spatial)
OVERPASS_QUERIES = (
    (
        f'nwr["amenity"~"^({"|".join(sorted(SCHOOL_AMENITIES))})$"]',
        "schools",
        "EDUCATIONAL_INSTITUTION",
        False,
    ),
    (
        'nwr["amenity"="hospital"]',
        "hospitals",
        "HEALTH_CARE_INSTITUTION",
        False,
    ),
    (
        'nwr["amenity"~"^(clinic|doctors|dentist)$"]',
        "clinics",
        None,
        False,
    ),
    (
        'nwr["amenity"="place_of_worship"]',
        "worship",
        "PLACE_OF_WORSHIP",
        False,
    ),
    (
        f'nwr["leisure"~"^({"|".join(sorted(PARK_LEISURE))})$"]',
        "parks",
        None,
        False,
    ),
    (
        'nwr["landuse"~"^(industrial|quarry|cemetery)$"]',
        "landuse",
        None,
        False,
    ),
    (
        'way["building"](around:2500,22.4468,114.1754)',
        "taipo_buildings",
        None,
        True,
    ),
)


def classify(tags: dict) -> tuple[str, str] | None:
    """Map OSM tags onto the schema's main_type enum, or None to skip."""
    amenity = tags.get("amenity")
    if amenity in SCHOOL_AMENITIES:
        return "PUBLIC", amenity
    if amenity in HOSPITAL_AMENITIES:
        return "PUBLIC", amenity
    if amenity == "place_of_worship":
        return "RELIGIOUS", tags.get("religion") or "place_of_worship"

    building = tags.get("building")
    if building and building != "no":
        return "BUILDING", building

    leisure = tags.get("leisure")
    if leisure in PARK_LEISURE:
        return "LEISURE", leisure

    if tags.get("natural") == "water" or tags.get("waterway"):
        return "WATER", tags.get("waterway") or tags.get("natural") or "water"

    if tags.get("landuse") in {"industrial", "quarry"}:
        return "INDUSTRIAL", tags.get("landuse")

    if tags.get("landuse") == "cemetery" or amenity == "grave_yard":
        return "CEMETERY", "cemetery"

    if tags.get("shop"):
        return "COMMERCIAL", tags.get("shop")

    return None


# ------------------------------------------------------------------------ GeoJSON


def shape_of(coords: list[tuple[float, float]], closed: bool):
    if len(coords) < 2:
        return None
    if closed and len(coords) >= 4:
        polygon = Polygon(coords)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.area == 0:
            return None
        return polygon, "POLYGON"
    line = LineString(coords)
    if line.is_empty or line.length == 0:
        return None
    return line, "LINESTRING"


class Collector(osmium.SimpleHandler):
    def __init__(self, flush, max_ways: int | None = None):
        super().__init__()
        self.flush = flush
        self.max_ways = max_ways
        self.ways = 0
        self.nodes = 0
        self.skipped = 0
        self.batch: list[tuple] = []

    def _emit(self, tags, geometry, geometry_type: str) -> None:
        hit = classify(tags)
        if hit is None:
            self.skipped += 1
            return
        main_type, sub_type = hit
        name = (tags.get("name:en") or tags.get("name") or "").strip()[:255] or None
        self.batch.append((name, str(sub_type)[:100], geometry.wkb_hex, geometry_type, main_type))
        if len(self.batch) >= 2000:
            self.flush(self.batch)
            self.batch = []

    def node(self, node) -> None:
        if self.max_ways is not None and self.ways >= self.max_ways:
            return
        if not node.tags or classify(node.tags) is None:
            return
        location = node.location
        if not location.valid():
            return
        self.nodes += 1
        self._emit(node.tags, Point(location.lon, location.lat), "POINT")

    def way(self, way) -> None:
        if self.max_ways is not None and self.ways >= self.max_ways:
            return
        if not way.tags or classify(way.tags) is None:
            return
        coords: list[tuple[float, float]] = []
        for ref in way.nodes:
            location = ref.location
            if not location.valid():
                return
            coords.append((location.lon, location.lat))
        if not coords:
            return
        shape = shape_of(coords, way.is_closed())
        if shape is None:
            return
        geometry, geometry_type = shape
        self.ways += 1
        self._emit(way.tags, geometry, geometry_type)


def load_pbf(raw, path: str, max_ways: int | None) -> int:
    sent = 0
    with raw.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {GEOMETRY_INDEX}")
        cursor.execute("DELETE FROM osm_feature")
    log("  cleared osm_feature, dropped the GIST index for the bulk load")

    def flush(batch: list[tuple]) -> None:
        nonlocal sent
        sent += insert_rows(raw, "osm_feature", OSM_COLUMNS, batch, template=OSM_TEMPLATE,
                            page_size=2000)
        if sent % 50000 < 2000:
            log(f"  {sent:,} rows")

    handler = Collector(flush, max_ways=max_ways)
    log("  parsing PBF (locations=True)")
    handler.apply_file(path, locations=True)
    if handler.batch:
        flush(handler.batch)

    with raw.cursor() as cursor:
        log("  rebuilding the GIST index")
        cursor.execute(f"CREATE INDEX {GEOMETRY_INDEX} ON osm_feature USING gist (geometry)")
    log(f"  ways={handler.ways:,} nodes={handler.nodes:,} skipped={handler.skipped:,}")
    return sent


# ----------------------------------------------------------------------- Overpass


def overpass(body: str, bbox: str, slug: str, is_raw: bool = False, refresh: bool = False) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE / f"overpass_{slug}.json"
    if cache_path.exists() and not refresh:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            elements = data.get("elements", [])
            log(f"  cache hit overpass_{slug}.json ({len(elements):>5} elements)")
            return elements
        except Exception:
            pass  # corrupt cache, refetch

    if is_raw:
        query = f"[out:json][timeout:180];{body};out center;"
    else:
        query = f"[out:json][timeout:180];{body}({bbox});out center;"

    for attempt, endpoint in enumerate(OVERPASS_ENDPOINTS * 3, start=1):
        started = time.perf_counter()
        try:
            import httpx

            response = httpx.post(
                endpoint, data={"data": query}, timeout=300,
                headers={"User-Agent": "neuro-air-lite/0.1 (+day-2 ingest)"},
            )
            if response.status_code == 200:
                data = response.json()
                elements = data.get("elements", [])
                cache_path.write_text(json.dumps({"elements": elements}), encoding="utf-8")
                log(f"  overpass {slug:<16} {len(elements):>5} elements ({time.perf_counter() - started:.1f}s)")
                return elements
            log(f"  overpass {slug:<16} HTTP {response.status_code}, retrying (endpoint: {endpoint})")
        except Exception as exc:  # noqa: BLE001
            log(f"  overpass {slug:<16} {type(exc).__name__}: {exc}, retrying")
        time.sleep(5)
    log(f"  overpass {slug:<16} gave up after retries")
    return []


def load_overpass(raw, bbox: str, refresh: bool = False) -> int:
    feature_rows: list[tuple] = []
    area_rows: list[tuple] = []

    for body, slug, area_category, is_raw in OVERPASS_QUERIES:
        elements = overpass(body, bbox, slug, is_raw=is_raw, refresh=refresh)
        query_features = 0
        query_areas = 0
        for element in elements:
            tags = element.get("tags") or {}
            centre = element.get("center") or {}
            lat = centre.get("lat", element.get("lat"))
            lon = centre.get("lon", element.get("lon"))
            if lat is None or lon is None:
                continue

            name = (tags.get("name:en") or tags.get("name") or "").strip()[:255] or None
            point = Point(lon, lat)

            if slug == "taipo_buildings":
                bldg = tags.get("building") or "yes"
                main_type = "BUILDING"
                sub_type = str(bldg)[:100]
                feature_rows.append((name, sub_type, point.wkb_hex, "POINT", main_type))
                query_features += 1
            else:
                hit = classify(tags)
                if hit is None:
                    continue
                main_type, sub_type = hit
                feature_rows.append((name, str(sub_type)[:100], point.wkb_hex, "POINT", main_type))
                query_features += 1

                if area_category:
                    if area_category == "HEALTH_CARE_INSTITUTION" and tags.get("amenity") != "hospital":
                        continue
                    area_rows.append((area_category, name, point.wkb_hex, "POINT"))
                    query_areas += 1

        log(f"    -> loaded {query_features:>5} osm_features, {query_areas:>5} sensitive_areas from {slug}")

    with raw.cursor() as cursor:
        cursor.execute(
            "DELETE FROM sensitive_area WHERE category IN "
            "('EDUCATIONAL_INSTITUTION', 'HEALTH_CARE_INSTITUTION', 'PLACE_OF_WORSHIP')"
        )
        cursor.execute("DELETE FROM osm_feature")

    sent = insert_rows(raw, "osm_feature", OSM_COLUMNS, feature_rows, page_size=1000)
    inserted = insert_rows(raw, "sensitive_area", AREA_COLUMNS, area_rows, template=AREA_TEMPLATE,
                           page_size=1000)
    log(f"  Total: {sent:,} osm_feature rows, {inserted:,} sensitive_area rows inserted")
    return sent


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("auto", "pbf", "overpass"), default="auto")
    parser.add_argument("--refresh", action="store_true", help="re-download the PBF or refresh Overpass cache")
    parser.add_argument("--pbf", help="path to a local .osm.pbf")
    parser.add_argument("--max-ways", type=int, default=None, help="stop after N ways (debug)")
    parser.add_argument(
        "--bbox", default="22.1193,113.8196,22.5597,114.4345",
        help="south,west,north,east for the Overpass queries",
    )
    args = parser.parse_args(argv)

    load_env()
    engine = make_admin_engine()
    raw = engine.raw_connection()
    try:
        source = args.source
        cached = CACHE / PBF_NAME
        if source == "auto":
            source = "pbf" if (args.pbf or cached.exists()) else "overpass"
            log(f"  source auto -> {source}")

        if source == "pbf":
            if args.pbf:
                path = args.pbf
            elif cached.exists() and not args.refresh:
                path = str(cached)
            else:
                fetch(PBF_URL, filename=PBF_NAME, refresh=args.refresh, timeout=900)
                path = str(cached)
            log(f"  PBF: {path}")
            load_pbf(raw, path, args.max_ways)
        else:
            load_overpass(raw, args.bbox, refresh=args.refresh)

        raw.commit()
    except Exception:
        raw.rollback()
        log("  rolled back, tables left unchanged")
        raise
    finally:
        raw.close()
        engine.dispose()

    log("  done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
