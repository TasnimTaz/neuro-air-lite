"""Runtime injection and host-side tools for Hong Kong domain agents.

Injects:
  engine:  SQLAlchemy Engine connected with read-only 'neuro_ro' role and hard statement timeout.
  pd:      pandas module.
  np:      numpy module.
  text:    sqlalchemy.text helper for writing SQL queries.
  geocode: host-side Hong Kong geocoder (Nominatim, ALS fallback).
  charts:  EChartsConfigManager; the agent builds chart configs, the UI renders them.

The tools are defined in the host process and injected as variables. Model code
can call `geocode(...)` but cannot `import httpx` or `import requests`, so the
network stays outside the sandbox.

Strict Isolation:
  DB_URL_ADMIN and the admin engine are NEVER injected or exposed to this
  namespace, and `load_agent_env()` removes the admin DSN from `os.environ` so a
  module that already holds `os` (pandas does) cannot read it either.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Any

import httpx
import numpy as np
import pandas as pd
from sqlalchemy import text

from core.env import load_agent_env
from core.runtime import PythonRuntime
from domains.hk.db import make_ro_engine
from domains.hk.viz import EChartsConfigManager

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ALS_URL = "https://www.als.gov.hk/lookup"
USER_AGENT = "neuro-air-lite/0.1 (HK air-quality research)"

# Hong Kong bounding box: lat_min, lat_max, lon_min, lon_max.
HK_BOUNDS = (22.15, 22.58, 113.83, 114.51)

# Public services: at most one request per second across both providers.
_MIN_INTERVAL = 1.0
_last_request = [0.0]


def _throttle() -> None:
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def _in_hong_kong(lat: float, lon: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = HK_BOUNDS
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def _nominatim(address: str) -> dict[str, Any] | None:
    """Nominatim search, filtered by the HK bounding box.

    Do NOT pass `countrycodes=hk`: on the public Nominatim instance that filter
    returns zero results for every Hong Kong address (verified 2026-11-09),
    while the same query without it returns the expected place. The bbox is the
    actual HK filter.
    """
    _throttle()
    response = httpx.get(
        NOMINATIM_URL,
        params={
            "q": address,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
            "accept-language": "en",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=15.0,
    )
    response.raise_for_status()
    try:
        hits = response.json()
    except ValueError:
        return None
    if not isinstance(hits, list):
        return None
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        try:
            lat, lon = float(hit["lat"]), float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if _in_hong_kong(lat, lon):
            return {
                "latitude": lat,
                "longitude": lon,
                "source": "nominatim",
                "label": hit.get("display_name") or address,
                # Nominatim does not publish a comparable match score.
                "score": None,
            }
    return None


def _als(address: str) -> dict[str, Any] | None:
    """Lands Department Address Lookup Service, the official HK geocoder.

    Nominatim occasionally misses building names or Chinese-only strings; ALS is
    authoritative for Hong Kong addresses and needs no API key.
    """
    _throttle()
    response = httpx.get(
        ALS_URL,
        params={"q": address, "n": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=15.0,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)

    lat_el = root.find(".//GeospatialInformation/Latitude")
    lon_el = root.find(".//GeospatialInformation/Longitude")
    if lat_el is None or lon_el is None or not lat_el.text or not lon_el.text:
        return None
    try:
        lat, lon = float(lat_el.text), float(lon_el.text)
    except ValueError:
        return None
    if not _in_hong_kong(lat, lon):
        return None

    label = root.findtext(".//BuildingName") or root.findtext(".//AddressLine") or address
    score_text = root.findtext(".//ValidationInformation/Score")
    try:
        score = float(score_text) if score_text else None
    except ValueError:
        score = None
    return {
        "latitude": lat,
        "longitude": lon,
        "source": "als",
        "label": label.strip(),
        # ALS always returns its best fuzzy candidate; a low score means the
        # match is weak. Not a boolean confidence: see the README caveat.
        "score": score,
    }


@lru_cache(maxsize=256)
def _geocode_cached(query: str) -> dict[str, Any]:
    try:
        hit = _nominatim(query)
    except httpx.HTTPError:
        hit = None
    if hit is None:
        try:
            hit = _als(query)
        except (httpx.HTTPError, ET.ParseError):
            hit = None
    if hit is None:
        raise ValueError(f"could not geocode {query!r} inside Hong Kong")
    return hit


def geocode(address: str) -> dict[str, Any]:
    """Hong Kong address -> {latitude, longitude, source, label, score}.

    Nominatim is tried first, then the Lands Department's official Address
    Lookup Service. Requests are throttled to one per second and results are
    cached for the process lifetime. Raises ValueError when neither source finds
    a candidate inside Hong Kong, so the agent sees a clear failure instead of an
    IndexError from an empty result list.

    Caveat: ALS is a fuzzy matcher and returns its best candidate even for
    nonsense input, so `score` is included for the caller to judge. A low score
    means a weak match; it is not a boolean confidence. Nominatim results carry
    `score=None` because Nominatim has no comparable score.
    """
    query = (address or "").strip()
    if not query:
        raise ValueError("geocode() needs a non-empty address")
    # Copy so a caller cannot mutate the cached dict.
    return dict(_geocode_cached(query))


def build_runtime(statement_timeout_ms: int | None = None) -> PythonRuntime:
    """Build a fresh PythonRuntime wired with the read-only engine and host tools."""
    load_agent_env()
    engine = make_ro_engine(statement_timeout_ms=statement_timeout_ms)
    variables = {
        "engine": engine,
        "pd": pd,
        "np": np,
        "text": text,
        "geocode": geocode,
        "charts": EChartsConfigManager(),
    }
    return PythonRuntime(variables=variables)
