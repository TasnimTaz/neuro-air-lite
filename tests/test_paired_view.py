"""Regression tests for the paired air-quality/weather view.

The old failure was a join on ``air_quality_station.id =
hourly_station_weather.station_id``.  Those ids belong to different sequences;
the weather station is paired by the ``wx_<station_code>`` prefix.  The view
``paired_hourly_weather`` makes the correct relation explicit.

These tests need the live database and the migration from
``scripts/migrate_views.py``.  They skip when the database is unavailable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from domains.hk.db import make_ro_engine

VIEW_ROWS_SQL = text(
    """
    SELECT count(*) AS n
    FROM paired_hourly_weather
    WHERE aq_station_code = 'taipo'
      AND datetime >= '2025-11-26 00:00:00'
      AND datetime < '2025-11-27 00:00:00'
    """
)

WIND_SQL = text(
    """
    SELECT wind_speed
    FROM paired_hourly_weather
    WHERE aq_station_code = 'taipo'
      AND datetime = '2025-11-26 20:00:00'
    """
)

DIRECT_JOIN_SQL = text(
    """
    SELECT count(*) AS n
    FROM air_quality_station a
    JOIN hourly_station_weather w ON a.id = w.station_id
    WHERE a.station_code = 'taipo'
      AND w.datetime >= '2025-11-26 00:00:00'
      AND w.datetime < '2025-11-27 00:00:00'
    """
)


@pytest.fixture(scope="module")
def engine():
    try:
        candidate = make_ro_engine()
    except KeyError:
        pytest.skip("DB_URL_RO is not configured")
    try:
        with candidate.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as error:
        candidate.dispose()
        pytest.skip(f"database unavailable: {error}")
    try:
        yield candidate
    finally:
        candidate.dispose()


def test_paired_view_has_24_taipo_hours_and_the_20_00_wind(engine):
    with engine.connect() as conn:
        count = conn.execute(VIEW_ROWS_SQL).scalar_one()
        wind = conn.execute(WIND_SQL).scalar_one()

    assert count == 24
    assert wind == pytest.approx(0.389, abs=0.001)


def test_direct_aq_to_weather_station_id_join_is_the_old_trap(engine):
    with engine.connect() as conn:
        view_count = conn.execute(VIEW_ROWS_SQL).scalar_one()
        direct_count = conn.execute(DIRECT_JOIN_SQL).scalar_one()

    # The ids are unrelated sequences, so the direct join does not produce the
    # same station's 24 hours. On this snapshot it is empty.
    assert direct_count != view_count
    assert direct_count == 0
