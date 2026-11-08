"""Day 2 step 0: find out which data endpoints actually serve 2025 hourly data.

    uv run --with pypdf python scripts/probe_sources.py
    uv run --with pypdf python scripts/probe_sources.py --section epd

Read-only: this script never touches the database and writes nothing to disk.
It is deliberately standalone so it can run before any ingest code exists.

Nothing here is trusted by the ingest scripts. This is only reconnaissance, and
every URL it prints still has to be re-verified by the ingest run itself.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time

import httpx

HK_BBOX = (22.1193, 113.8196, 22.5597, 114.4345)  # south, west, north, east
UA = {"User-Agent": "neuro-air-lite/0.1 (day-2 source probe)"}


def fetch(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 45.0,
    retries: int = 3,
    data: dict | None = None,
) -> httpx.Response | None:
    """GET/POST with retries. The government hosts drop connections at random."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        try:
            response = httpx.request(
                method, url, timeout=timeout, headers=UA, data=data, follow_redirects=True
            )
            elapsed = time.perf_counter() - started
            print(f"      attempt {attempt}: {response.status_code} "
                  f"{len(response.content)} B in {elapsed:.1f}s")
            if response.status_code < 500:
                return response
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - started
            print(f"      attempt {attempt}: {type(exc).__name__} after {elapsed:.1f}s")
            last = exc
        time.sleep(2 * attempt)
    if last:
        print(f"      gave up: {type(last).__name__}: {last}")
    return None


def section_hko() -> None:
    print("\n=== HKO: which dataType has hourly weather? ===")
    doc_url = "https://data.weather.gov.hk/weatherAPI/doc/HKO_Open_Data_API_Documentation.pdf"
    print(f"  [doc] {doc_url}")
    response = fetch(doc_url, timeout=90)
    if response is None:
        print("  [FAIL] could not fetch the API documentation")
        return

    try:
        import pypdf
    except ImportError:
        print("  [skip] pypdf not installed; run with: uv run --with pypdf ...")
        return

    reader = pypdf.PdfReader(io.BytesIO(response.content))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    print(f"  parsed {len(reader.pages)} pages, {len(text)} chars")

    types = sorted(set(re.findall(r"dataType\s*\n?\s*([A-Z][A-Z0-9_]{1,12})", text)))
    print(f"  dataType values: {', '.join(types) or '(none found)'}")

    print("  dataset titles mentioning hourly:")
    seen: set[str] = set()
    for match in re.finditer(r"[Hh]ourly[^\n]{0,90}", text):
        line = " ".join(match.group(0).split())
        if line not in seen:
            seen.add(line)
            print(f"    - {line}")
        if len(seen) >= 12:
            break

    print("  live test of real-time and rainfall endpoints:")
    for url in (
        "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en",
        "https://data.weather.gov.hk/weatherAPI/opendata/hourlyRainfall.php?lang=en",
    ):
        print(f"  [get] {url}")
        response = fetch(url, timeout=60)
        if response is not None:
            print(f"      {response.text[:180]}")


def section_epd() -> None:
    print("\n=== EPD: Air Quality Data Repository (EPIC) ===")
    # HTTPS on this host has a broken certificate chain and times out; plain HTTP works.
    page_url = "http://cd.epic.epd.gov.hk/EPICDI/air/station/?lang=en"
    print(f"  [get] {page_url}")
    response = fetch(page_url, timeout=60, retries=4)
    if response is None:
        print("  [FAIL] EPIC page unreachable, EPD historical data needs a fallback")
        return

    html = response.text
    title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    print(f"  title: {title.group(1).strip() if title else '(none)'}")
    print(f"  html: {len(html)} chars")

    scripts = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', html)))
    links = sorted(set(re.findall(r'href="([^"]+)"', html)))
    print(f"  scripts ({len(scripts)}):")
    for item in scripts[:12]:
        print(f"    - {item}")
    print(f"  links ({len(links)}):")
    for item in links[:20]:
        print(f"    - {item}")


def section_osm() -> None:
    print("\n=== OSM: Geofabrik extract and Overpass ===")
    pbf = "https://download.geofabrik.de/asia/hong-kong-latest.osm.pbf"
    print(f"  [head] {pbf}")
    response = fetch(pbf, method="HEAD", timeout=60)
    if response is not None:
        size = response.headers.get("content-length")
        print(f"      size: {int(size) / 1e6:.1f} MB" if size else "      size: unknown")
        print(f"      last-modified: {response.headers.get('last-modified')}")

    south, west, north, east = HK_BBOX
    for label, selector in (("school", '["amenity"="school"]'), ("hospital", '["amenity"="hospital"]')):
        query = (
            f"[out:json][timeout:120];"
            f"nwr{selector}({south},{west},{north},{east});out count;"
        )
        print(f"  [overpass] {label}")
        response = fetch(
            "https://overpass-api.de/api/interpreter", method="POST", data={"data": query}, timeout=180
        )
        if response is not None:
            print(f"      {response.text[:200]}")

    query = (
        f"[out:json][timeout:120];"
        f'way["building"]({south},{west},{north},{east});out count;'
    )
    print("  [overpass] building (this is the expensive one)")
    response = fetch(
        "https://overpass-api.de/api/interpreter", method="POST", data={"data": query}, timeout=240
    )
    if response is not None:
        print(f"      {response.text[:200]}")


SECTIONS = {"hko": section_hko, "epd": section_epd, "osm": section_osm}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--section", choices=sorted(SECTIONS), action="append", help="run only this section"
    )
    args = parser.parse_args(argv)

    chosen = args.section or ["epd", "hko", "osm"]
    print("neuro-air-lite :: Day 2 source probe")
    print(f"HK bbox: {HK_BBOX}")
    for name in chosen:
        SECTIONS[name]()

    print("\nDone. Record what worked before writing any ingest code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
