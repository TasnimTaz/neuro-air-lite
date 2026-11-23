"""Unit tests for the external HK-12 adapter compatibility shim."""

from __future__ import annotations

from evals.paper_hk12 import (
    PAPER_FROM_LOCAL_STATION,
    PAPER_TO_LOCAL_STATION,
    PAPER_TO_LOCAL_STATION as MAP,
    ShimRuntime,
    paper_text,
)


def test_station_map_is_bijective_and_complete():
    assert len(MAP) == 18
    assert set(MAP) == set(range(1, 19))
    assert len(set(MAP.values())) == 18
    assert PAPER_FROM_LOCAL_STATION == {v: k for k, v in MAP.items()}
    assert PAPER_TO_LOCAL_STATION[16] == 35  # Central
    assert PAPER_TO_LOCAL_STATION[17] == 34  # Causeway Bay


def test_paper_text_rewrites_only_paper_station_id_equalities():
    sql = (
        "SELECT * FROM hourly_station_air_quality WHERE station_id = 17 "
        "AND datetime >= '2025-06-15' AND station_id IN (16, 99)"
    )
    rewritten = paper_text(sql).text
    assert "station_id = 34" in rewritten
    assert "station_id IN (16, 99)" in rewritten  # numeric IN lists are not used by these validators
    assert "station_id = 99" not in rewritten


def test_paper_text_leaves_local_station_ids_untouched():
    rewritten = paper_text("SELECT * FROM hourly_station_air_quality WHERE station_id = 34").text
    assert "station_id = 34" in rewritten


def test_runtime_maps_only_higher_peak_station_id():
    rt = ShimRuntime({"higher_peak_station_id": 35, "most_polluted_station_id": 34, "x": 16})
    assert rt.retrieve("higher_peak_station_id") == 16
    assert rt.retrieve("most_polluted_station_id") == 34
    assert rt.retrieve("x") == 16
    assert rt.retrieve("missing") is None
