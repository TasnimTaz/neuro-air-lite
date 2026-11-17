"""Pure and optional live tests for the Day 8 Tai Po reference."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from analysis.daemon import ReferenceDaemon
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
