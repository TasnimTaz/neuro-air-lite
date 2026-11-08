"""Shared helpers for the ingest scripts.

Every source is flaky: the Hong Kong government hosts drop connections and the
Open-Meteo endpoints rate-limit. So:

* every download goes through `fetch`, which retries with backoff and caches the
  raw bytes under `ingest/cache/` (gitignored). A re-run reads from disk, so a
  half-finished ingest can always be resumed without re-downloading.
* every insert is an upsert or a transaction, so a re-run converges to the same
  rows instead of duplicating them.
"""

from __future__ import annotations

import hashlib
import sys
import time
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "ingest" / "cache"

USER_AGENT = "neuro-air-lite/0.1 (+day-2 ingest)"


def log(message: str) -> None:
    print(message, flush=True)


def load_env() -> None:
    """Load .env once, before any DB or API call."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)


def fetch(
    url: str,
    *,
    filename: str | None = None,
    refresh: bool = False,
    timeout: float = 180.0,
    retries: int = 5,
) -> bytes:
    """Download `url` into the cache and return the bytes.

    Cached files are keyed by the caller-supplied filename so a month's CSV keeps
    a readable name on disk.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    if filename is None:
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        filename = f"{digest}.bin"
    path = CACHE / filename

    if path.exists() and not refresh:
        log(f"  cache hit  {filename} ({path.stat().st_size:,} B)")
        return path.read_bytes()

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        try:
            response = httpx.get(
                url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"  retry {attempt}/{retries} after {type(exc).__name__} ({url})")
            time.sleep(min(2 ** attempt, 15))
            continue

        elapsed = time.perf_counter() - started
        part_path = CACHE / f"{filename}.part"
        part_path.write_bytes(response.content)
        part_path.replace(path)
        log(f"  fetched    {filename} ({len(response.content):,} B in {elapsed:.1f}s)")
        return response.content

    raise RuntimeError(f"could not download {url} after {retries} attempts: {last}")


def insert_rows(
    raw_connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    *,
    on_conflict: str = "",
    template: str | None = None,
    page_size: int = 1000,
) -> int:
    """Upsert `rows` with psycopg2's execute_values. Returns the row count sent."""
    from psycopg2.extras import execute_values

    rows = list(rows)
    if not rows:
        return 0

    column_sql = ", ".join(f'"{column}"' for column in columns)
    sql = f'INSERT INTO "{table}" ({column_sql}) VALUES %s {on_conflict}'.strip()

    with raw_connection.cursor() as cursor:
        execute_values(cursor, sql, rows, template=template, page_size=page_size)
    return len(rows)


def month_keys(start: date, end: date) -> list[tuple[int, int]]:
    """Every (year, month) touched by the inclusive date range."""
    keys: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append((year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return keys


def day_chunks(start: date, end: date, size: int = 31) -> list[tuple[date, date]]:
    """Split an inclusive date range into <= `size` day windows (API limits)."""
    windows: list[tuple[date, date]] = []
    cursor = start
    from datetime import timedelta

    while cursor <= end:
        stop = min(cursor + timedelta(days=size - 1), end)
        windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return windows


def fail(message: str) -> int:
    print(f"[FAIL] {message}", file=sys.stderr)
    return 1
