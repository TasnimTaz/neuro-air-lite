import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from domains.hk import roles, roles_v5
from evals.v5 import tasks as T

ENT = {
    "stations": [
        {"name": n, "type": t, "area": "URBAN"}
        for n, t in [("Central/Western", "GENERAL"), ("Sha Tin", "GENERAL"), ("Tung Chung", "GENERAL"),
                     ("Causeway Bay", "ROADSIDE"), ("Mong Kok", "ROADSIDE"), ("Tai Po", "GENERAL")]
    ],
    "paired": ["Sha Tin", "Tung Chung", "Tai Po"],
}


def test_build_is_deterministic_unique_and_stratified():
    a = T.build_tasks(ENT, 160, seed=1)
    b = T.build_tasks(ENT, 160, seed=1)
    assert [t.question for t in a] == [t.question for t in b]
    assert len({t.question for t in a}) == len(a)
    cats = {t.category for t in a}
    assert cats == {"retrieval", "temporal", "statistics", "comparative", "spatial", "multitable"}
    assert 0.1 < sum(t.split == "dev" for t in a) / len(a) < 0.35
    assert all(len(t.outputs) >= 1 for t in a)


def test_variable_names_are_unique_and_valid_identifiers():
    for t in T.build_tasks(ENT, 160, seed=2):
        names = t.var_names()
        assert len(set(names)) == len(names)
        assert all(n.isidentifier() for n in names), names


def test_all_truth_sql_parses_as_postgres():
    pglast = pytest.importorskip("pglast")
    tasks = T.build_tasks(ENT, 200, seed=3)
    for t in tasks:
        for o in t.outputs:
            pglast.parse_sql(o.truth_sql)  # raises on a syntax error


def test_station_quote_is_escaped():
    ent = {"stations": [{"name": "O'Brien", "type": "GENERAL", "area": "URBAN"}], "paired": ["O'Brien"]}
    seen = 0
    for t in T.build_tasks(ent, 60, seed=4):
        for o in t.outputs:
            assert "O'Brien" not in o.truth_sql  # never an unescaped quote inside SQL
            seen += "O''Brien" in o.truth_sql
    assert seen > 0


def _task(kind, expected, **kw):
    o = T.Output(var="v", desc="d", kind=kind, truth_sql="select 1", expected=expected, **kw)
    return T.Task(id=1, family="f", category="retrieval", difficulty="simple", question="q", outputs=(o,))


@pytest.mark.parametrize(
    "kind,expected,got,ok,kw",
    [
        ("float", 10.0, 10.04, True, {"tol_abs": 0.05}),
        ("float", 10.0, 10.2, False, {"tol_abs": 0.05, "tol_rel": 0.005}),
        ("float", 100.0, 100.4, True, {"tol_abs": 0.01, "tol_rel": 0.005}),
        ("float", 1.0, np.float64(1.0), True, {}),
        ("float", 1.0, float("nan"), False, {}),
        ("float", 1.0, "1.0", False, {}),
        ("float", 1.0, True, False, {}),
        ("int", 31, 31.0, True, {}),
        ("int", 31, 30, False, {}),
        ("int", 31, np.int64(31), True, {}),
        ("text", "Central/Western", "central western", True, {}),
        ("text", "Tai Po", "Tai Wai", False, {}),
        ("text", "Tai Po", 3, False, {}),
    ],
)
def test_check_output(kind, expected, got, ok, kw):
    assert T.validate({"v": got}, _task(kind, expected, **kw)).success is ok


def test_missing_variable_fails_and_series_dataframe_are_unwrapped():
    t = _task("float", 2.5, tol_abs=0.01)
    assert not T.validate({}, t).success
    assert T.validate({"v": pd.Series([2.5])}, t).success
    assert T.validate({"v": pd.DataFrame({"x": [2.5]})}, t).success
    assert not T.validate({"v": pd.Series([2.5, 3.0])}, t).success


def test_all_outputs_must_match():
    a = T.Output("a", "d", "int", "s", expected=1)
    b = T.Output("b", "d", "int", "s", expected=2)
    t = T.Task(1, "f", "retrieval", "simple", "q", (a, b))
    assert T.validate({"a": 1, "b": 2}, t).success
    assert not T.validate({"a": 1, "b": 3}, t).success


def test_freeze_drops_null_error_nan_and_all_zero_and_pins_truth(tmp_path: Path):
    tasks = T.build_tasks(ENT, 40, seed=5)
    calls = {"n": 0}

    def run(sql):
        calls["n"] += 1
        k = calls["n"] % 6
        if k == 0:
            return None
        if k == 1:
            raise RuntimeError("boom")
        if k == 2:
            return float("nan")
        return "Sha Tin" if "name_en" in sql.split("FROM")[0] and "SELECT s" in sql[:12] else 3

    kept, dropped = T.freeze(tasks, run)
    assert kept and dropped
    assert all(o.expected is not None for t in kept for o in t.outputs)
    assert [t.id for t in kept] == list(range(1, len(kept) + 1))
    digest = T.save_tasks(tmp_path / "t.json", kept, seed=5, dropped=dropped)
    assert [t.question for t in T.load_tasks(tmp_path / "t.json")] == [t.question for t in kept]
    # tamper -> hash check fails
    p = tmp_path / "t.json"
    p.write_text(p.read_text().replace('"expected": 3', '"expected": 4', 1))
    with pytest.raises(ValueError, match="modified after it was frozen"):
        T.load_tasks(p)
    assert len(digest) == 64


def test_output_contract_names_every_variable():
    t = T.build_tasks(ENT, 30, seed=6)[0]
    text = T.output_contract(t)
    assert all(f"`{v}`" in text for v in t.var_names())
    assert "ANSWER" not in text


def test_v5_roles_have_no_answer_variable_and_keep_v4_procedures():
    blob = roles_v5.COORDINATOR_SYSTEM + roles_v5.MULTI_RULES + "".join(roles_v5.ROLE_PROMPTS.values()) + roles_v5.MERGED_ROLE_ADDENDUM_V5
    assert "ANSWER" not in blob
    assert set(roles_v5.ROLE_PROMPTS) == set(roles.ROLE_PROMPTS)
    assert "required output variables" in roles_v5.ROLE_PROMPTS["synthesis_report"]
    # only wording about the answer variable differs
    for k in roles.ROLE_PROMPTS:
        assert len(roles_v5.ROLE_PROMPTS[k]) >= len(roles.ROLE_PROMPTS[k]) - 40
