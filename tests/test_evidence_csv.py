"""Re-derive the frozen v5 headline numbers from the committed CSV exports."""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> list[dict[str, str]]:
    with (ROOT / "docs/results" / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def yes(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def arm_map(rows: list[dict[str, str]], arm: str) -> dict[str, dict[str, str]]:
    return {r["task_id"]: r for r in rows if r["arm"] == arm}


def paired_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    single, multi = arm_map(rows, "single"), arm_map(rows, "multi")
    assert single.keys() == multi.keys()
    wins = sum(yes(m["pass"]) and not yes(s["pass"]) for s, m in zip(single.values(), multi.values()))
    losses = sum(yes(s["pass"]) and not yes(m["pass"]) for s, m in zip(single.values(), multi.values()))
    pre = sum(yes(m["pass_pre_verify"]) for m in multi.values())
    rescued = sum(yes(m["pass"]) and not yes(m["pass_pre_verify"]) for m in multi.values())
    broke = sum(yes(m["pass_pre_verify"]) and not yes(m["pass"]) for m in multi.values())
    return {
        "single_pass": sum(yes(r["pass"]) for r in single.values()),
        "multi_pass": sum(yes(r["pass"]) for r in multi.values()),
        "wins": wins,
        "losses": losses,
        "pre": pre,
        "rescued": rescued,
        "broke": broke,
    }


def test_v5_test_csv_rederives_headline_numbers():
    counts = paired_counts(load("v5_test_cases.csv"))
    assert counts == {
        "single_pass": 182,
        "multi_pass": 166,
        "wins": 11,
        "losses": 27,
        "pre": 90,
        "rescued": 77,
        "broke": 1,
    }
    assert counts["pre"] + counts["rescued"] - counts["broke"] == counts["multi_pass"]


def test_v5_dev_csv_rederives_diagnosis_numbers():
    counts = paired_counts(load("v5_dev_cases.csv"))
    assert counts == {
        "single_pass": 49,
        "multi_pass": 42,
        "wins": 2,
        "losses": 9,
        "pre": 27,
        "rescued": 15,
        "broke": 0,
    }


def test_csv_rows_have_required_columns_and_no_infra_failures():
    for name in ("v5_test_cases.csv", "v5_dev_cases.csv"):
        rows = load(name)
        expected_rows = 398 if "test" in name else 106
        assert len(rows) == expected_rows
        assert {r["arm"] for r in rows} == {"single", "multi"}
        assert all(not yes(r["infra_failed"]) for r in rows)
        assert all(r["task_id"] and r["category"] and r["difficulty"] for r in rows)
