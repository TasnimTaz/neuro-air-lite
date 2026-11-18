"""Pure and optional live tests for the Day 8 Tai Po reference."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from analysis.daemon import ReferenceDaemon, check_alerts, resolve_now
from analysis.tai_po_reference import (
    SPECS,
    SQL_STATEMENTS,
    compute_reference,
    evaluate_reference,
)
from domains.hk.db import make_ro_engine


def _passing_observed() -> dict[str, object]:
    return {spec.key: spec.expected for spec in SPECS}


def test_reference_passes_with_expected_values() -> None:
    checks = evaluate_reference(_passing_observed())
    assert len(checks) == len(SPECS)
    assert all(check.ok for check in checks)


def test_reference_flags_one_changed_value() -> None:
    observed = _passing_observed()
    observed["pm25_post_peak"] = 55.0
    checks = evaluate_reference(observed)
    failed = [check for check in checks if not check.ok]
    assert [check.key for check in failed] == ["pm25_post_peak"]


def test_reference_flags_missing_value() -> None:
    observed = _passing_observed()
    del observed["wind_speed_2000"]
    checks = evaluate_reference(observed)
    failed = [check.key for check in checks if not check.ok]
    assert failed == ["wind_speed_2000"]


def test_reference_sql_contains_no_write_verbs() -> None:
    forbidden = re.compile(
        r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke)\b",
        re.IGNORECASE,
    )
    for statement in SQL_STATEMENTS:
        sql = str(statement)
        assert not forbidden.search(sql), sql


def test_reference_source_enforces_read_only_transaction() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "analysis"
        / "tai_po_reference.py"
    ).read_text(encoding="utf-8")
    assert "SET TRANSACTION READ ONLY" in source


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_daemon_check_reports_fetch_error_and_disposes_engine() -> None:
    engine = _FakeEngine()

    def failing_fetch(_engine: object) -> dict[str, object]:
        raise RuntimeError("database unavailable")

    daemon = ReferenceDaemon(
        engine_factory=lambda: engine,
        fetch=failing_fetch,
        sleep=lambda _seconds: None,
    )
    snapshot = daemon.check_once()

    assert snapshot["ok"] is False
    assert snapshot["degraded"] is True
    assert "database unavailable" in snapshot["error"]
    assert engine.disposed is True


def test_daemon_run_honours_max_checks() -> None:
    engine = _FakeEngine()
    sleeps: list[float] = []
    emitted: list[str] = []

    daemon = ReferenceDaemon(
        engine_factory=lambda: engine,
        fetch=lambda _engine: {"ok": True, "checks": []},
        interval_seconds=0.0,
        max_checks=2,
        sleep=lambda seconds: sleeps.append(seconds),
        emit=emitted.append,
    )
    last = daemon.run()

    assert last["ok"] is True
    assert len(emitted) == 2
    assert sleeps == [0.0]
    assert engine.disposed is True


def test_live_tai_po_reference_if_database_is_available() -> None:
    try:
        engine = make_ro_engine()
    except KeyError:
        pytest.skip("DB_URL_RO is not configured")

    try:
        try:
            report = compute_reference(engine)
        except OperationalError as error:
            pytest.skip(f"database unavailable: {error}")
    finally:
        engine.dispose()

    assert report["ok"] is True, [
        check for check in report["checks"] if not check["ok"]
    ]
    assert report["observed"]["pm25_post_peak"] == pytest.approx(54.6)
    assert report["observed"]["no2_at_2000"] == pytest.approx(123.2)
    assert report["observed"]["aqhi_at_2000"] == 5
    assert report["observed"]["wind_direction_2000"] == "SW"
    assert report["observed"]["sensitive_education_station_1500"] == 53
    anomaly = report["anomaly_assessment"]
    assert anomaly["matched_hour_z"] == pytest.approx(1.91, abs=0.01)
    assert anomaly["matched_hour_max_prev_14d"] == pytest.approx(125.5)
    assert anomaly["network_mean_rise"] == pytest.approx(19.6, abs=0.1)
    assert anomaly["nearest_station_rise_vs_network"] == pytest.approx(7.4, abs=0.1)
    assert anomaly["pm25_rank_of_nearest"] == 1
    assert anomaly["supports_local_plume"] == "false"


def _synthetic_alert_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE air_quality_station ("
                "id INTEGER PRIMARY KEY, station_code TEXT, name_en TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE hourly_station_air_quality ("
                "station_id INTEGER, datetime TEXT, aqhi INTEGER, aqhi_level TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO air_quality_station (id, station_code, name_en) VALUES "
                "(1, 'taipo', 'Tai Po'), (2, 'shatin', 'Sha Tin'), "
                "(3, 'north', 'North'), (4, 'tapmun', 'Tap Mun')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO hourly_station_air_quality "
                "(station_id, datetime, aqhi, aqhi_level) VALUES "
                "(1, '2025-11-26 15:00:00', 6, 'HIGH'), "
                "(2, '2025-11-26 15:00:00', 8, 'VERY HIGH'), "
                "(3, '2025-11-26 15:00:00', 7, 'HIGH'), "
                "(4, '2025-11-26 14:00:00', 6, 'HIGH')"
            )
        )
    return engine


def test_check_alerts_uses_latest_hour_threshold_and_job_text() -> None:
    engine = _synthetic_alert_engine()
    jobs: list[str] = []
    try:
        with engine.connect() as conn:
            triggers = check_alerts(
                conn,
                "2025-11-26 15:30",
                threshold=7,
                cooldown=3600,
                state={},
                run_job=jobs.append,
            )
    finally:
        engine.dispose()

    assert [row["station_code"] for row in triggers] == ["north", "shatin"]
    assert all(row["aqhi"] >= 7 for row in triggers)
    assert jobs == [
        "Assess the air quality situation near North and nearby sensitive receptors.",
        "Assess the air quality situation near Sha Tin and nearby sensitive receptors.",
    ]


def test_check_alerts_honours_cooldown_and_new_latest_hour() -> None:
    engine = _synthetic_alert_engine()
    state: dict[str, object] = {}
    try:
        with engine.connect() as conn:
            first = check_alerts(
                conn,
                "2025-11-26 15:30",
                threshold=7,
                cooldown=3600,
                state=state,
                run_job=lambda _query: None,
            )
            second = check_alerts(
                conn,
                "2025-11-26 15:45",
                threshold=7,
                cooldown=3600,
                state=state,
                run_job=lambda _query: None,
            )
            conn.execute(
                text(
                    "INSERT INTO hourly_station_air_quality "
                    "(station_id, datetime, aqhi, aqhi_level) VALUES "
                    "(1, '2025-11-26 16:00:00', 9, 'SERIOUS')"
                )
            )
            third = check_alerts(
                conn,
                "2025-11-26 16:31",
                threshold=7,
                cooldown=3600,
                state=state,
                run_job=lambda _query: None,
            )
    finally:
        engine.dispose()

    assert [row["station_code"] for row in first] == ["north", "shatin"]
    assert second == []
    assert [row["station_code"] for row in third] == ["north", "shatin", "taipo"]
    taipo = next(row for row in third if row["station_code"] == "taipo")
    assert taipo["aqhi"] == 9


def test_resolve_now_uses_replay_now_and_never_wall_clock(monkeypatch) -> None:
    monkeypatch.setenv("REPLAY_NOW", "2025-11-26 15:30")
    assert resolve_now() == __import__("datetime").datetime(2025, 11, 26, 15, 30)


def test_live_alert_replay_if_database_is_available(monkeypatch) -> None:
    monkeypatch.setenv("REPLAY_NOW", "2025-11-26 15:30")
    try:
        engine = make_ro_engine()
    except KeyError:
        pytest.skip("DB_URL_RO is not configured")

    try:
        try:
            with engine.connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                triggers = check_alerts(
                    conn,
                    None,
                    threshold=7,
                    cooldown=3600,
                    state={},
                    run_job=lambda _query: None,
                )
                conn.rollback()
        except OperationalError as error:
            pytest.skip(f"database unavailable: {error}")
    finally:
        engine.dispose()

    assert all(row["aqhi"] >= 7 for row in triggers)
    assert all(row["observed_at"] <= "2025-11-26 15:30:00" for row in triggers)
    taipo = [row for row in triggers if row["station_code"] == "taipo"]
    assert all(row["aqhi"] >= 7 for row in taipo)
