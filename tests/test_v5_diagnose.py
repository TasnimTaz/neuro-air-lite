import json
from pathlib import Path

import pytest

from evals import diagnose_v5 as D


def case(i, ok=True, **kw):
    return {"task_id": i, "pass": ok, "category": "c", "difficulty": "simple", **kw}


def write_run(d: Path, cases):
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps({"cases": cases}))
    return d


def test_all_infra_failed_exits_with_clear_message(tmp_path):
    d = write_run(tmp_path / "a", [case(1, infra_failed=True), case(2, infra_failed=True)])
    with pytest.raises(SystemExit, match="every case is infra_failed"):
        D.load(d)


def test_repeated_task_id_is_refused_not_collapsed(tmp_path):
    d = write_run(tmp_path / "a", [case(1), case(1, ok=False), case(2)])
    with pytest.raises(SystemExit, match=r"appear more than once.*\[1\]"):
        D.load(d)


def test_infra_failed_duplicates_are_ignored(tmp_path):
    d = write_run(tmp_path / "a", [case(1, infra_failed=True), case(1), case(2)])
    assert sorted(D.load(d)) == [1, 2]


def test_cases_dir_fallback_and_missing_files(tmp_path):
    d = tmp_path / "a"
    (d / "cases").mkdir(parents=True)
    for i in (1, 2):
        (d / "cases" / f"task-{i:04d}.json").write_text(json.dumps(case(i)))
    assert sorted(D.load(d)) == [1, 2]
    with pytest.raises(SystemExit, match="neither"):
        D.load(tmp_path / "empty")


def test_run_json_without_cases_list(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"summary": {}}))
    with pytest.raises(SystemExit, match="no 'cases' list"):
        D.load(d)


def test_end_to_end_runs_on_minimal_schema(tmp_path, capsys):
    s = write_run(tmp_path / "s", [case(1), case(2), case(3, ok=False)])
    m = write_run(tmp_path / "m", [case(1), case(2, ok=False), case(3)])
    assert D.main(str(s), str(m)) == 0
    out = capsys.readouterr().out
    assert "[schema]" in out and "== 2. Why multi lost" in out and "skipped" in out
