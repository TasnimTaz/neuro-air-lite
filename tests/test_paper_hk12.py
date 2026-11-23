"""Unit tests for the external HK-12 adapter compatibility shim."""

from __future__ import annotations

from evals.paper_hk12 import (
    PAPER_FROM_LOCAL_STATION,
    PAPER_TO_LOCAL_STATION,
    PAPER_TO_LOCAL_STATION as MAP,
    ShimRuntime,
    crosswalk_manifest,
    crosswalk_station_ids,
    install_shims,
    load_tasks,
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


def test_crosswalk_maps_station_ids_but_not_other_numbers():
    text = (
        "Central station (station_id: 16) and Tuen Mun station (station_id: 5) "
        "from January 20-26, 2025, 08:00-14:00. "
        "North District (district_id: 3) has all 18 monitoring stations. "
        "higher_peak_station_id (16 or 5, higher max AQHI, int)."
    )
    out = crosswalk_station_ids(text)
    assert "station_id: 35" in out
    assert "station_id: 28" in out
    assert "Tuen Mun station 28" not in out  # the paper text has no bare "station 5" here
    assert "higher_peak_station_id (35 or 28" in out
    assert "district_id: 3" in out
    assert "all 18 monitoring stations" in out
    assert "2025" in out and "08:00-14:00" in out
    assert crosswalk_station_ids(out) == out  # idempotent


def test_crosswalk_maps_bare_station_references_and_leaves_local_ids():
    out = crosswalk_station_ids("for station 17 on 2025-06-15; station 34 is already local")
    assert "for station 34 on 2025-06-15" in out
    assert "station 34 is already local" in out


def test_vendored_task_question_uses_crosswalk_but_raw_is_unchanged():
    install_shims(object())  # no DB is needed to import the validator modules
    tasks = {t.name: t for t in load_tasks()}
    central = tasks["hk_1_13"]
    assert "station_id: 16" in central.question_raw
    assert "station_id: 35" in central.question
    assert "higher_peak_station_id (35 or 28" in central.question
    north = tasks["hk_2_18"]
    assert "district_id: 3" in north.question
    assert "station_id: 32" in north.question  # paper station 2 -> local North


def test_crosswalk_covers_every_station_id_in_all_12_questions():
    install_shims(object())
    tasks = {t.name: t for t in load_tasks()}
    expected = {
        "hk_1_13": [(16, 35), (5, 28)],
        "hk_1_5": [(6, 29), (8, 33)],
        "hk_1_6": [(16, 35), (5, 28)],
        "hk_1_10": [(17, 34)],
        "hk_1_7": [(17, 34)],
        "hk_2_18": [(2, 32)],
    }
    for name, pairs in expected.items():
        raw, translated = tasks[name].question_raw, tasks[name].question
        for paper, local in pairs:
            assert f"station_id: {paper}" in raw or f"station {paper}" in raw
            assert f"station_id: {local}" in translated or f"station {local}" in translated
            assert f"station_id: {paper}" not in translated
    # The six ID-free questions must be byte-identical after the crosswalk.
    for name in set(tasks) - set(expected):
        assert tasks[name].question == tasks[name].question_raw, name

    manifest = crosswalk_manifest(list(tasks.values()))
    assert len(manifest) == 12
    for name, entry in manifest.items():
        assert entry["raw"] == tasks[name].question_raw
        assert entry["translated"] == tasks[name].question


def test_runtime_maps_only_higher_peak_station_id():
    rt = ShimRuntime({"higher_peak_station_id": 35, "most_polluted_station_id": 34, "x": 16})
    assert rt.retrieve("higher_peak_station_id") == 16
    assert rt.retrieve("most_polluted_station_id") == 34
    assert rt.retrieve("x") == 16
    assert rt.retrieve("missing") is None
