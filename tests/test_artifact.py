"""Tests for the incremental artifact helpers used by --resume."""

from __future__ import annotations

from pathlib import Path

from evals.run_baseline import _case_path, _read_json, _write_json


def test_case_path_is_stable_and_repeat_scoped(tmp_path: Path):
    assert _case_path(tmp_path, 1, 3) == tmp_path / "cases" / "repeat-01-case-03.json"
    assert _case_path(tmp_path, 3, 20) == tmp_path / "cases" / "repeat-03-case-20.json"


def test_atomic_json_round_trip_and_corrupt_file_returns_none(tmp_path: Path):
    path = tmp_path / "cases" / "repeat-01-case-01.json"
    payload = {"id": 1, "pass": True, "answer": 20.0}

    _write_json(path, payload)

    assert _read_json(path) == payload
    assert not (tmp_path / "cases" / "repeat-01-case-01.json.tmp").exists()

    path.write_text("{not json", encoding="utf-8")
    assert _read_json(path) is None
