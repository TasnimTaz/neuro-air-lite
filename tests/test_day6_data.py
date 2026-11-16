"""Day 6 data-contract tests: gate set, premise traps and calibration split."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.calibrate_gate import (
    GateItem,
    ScoredItem,
    accuracy,
    confusion,
    load_items,
    select_theta,
    split_items,
)

ROOT = Path(__file__).resolve().parents[1]


def test_gate_set_has_the_required_groups():
    payload = json.loads((ROOT / "evals" / "gate_set.json").read_text(encoding="utf-8"))

    assert len(payload["in_scope"]) == 30
    assert len(payload["out_of_scope"]) == 30
    assert len(payload["vague_in_scope"]) == 10
    ids = [
        entry["id"]
        for group in ("in_scope", "out_of_scope", "vague_in_scope")
        for entry in payload[group]
    ]
    assert len(ids) == len(set(ids)) == 70


def test_calibration_split_is_stratified_and_disjoint():
    items = load_items(ROOT / "evals" / "gate_set.json")
    train, test = split_items(items)

    assert len(train) == len(test) == 35
    assert {item.id for item in train}.isdisjoint({item.id for item in test})
    for group, expected in (("in_scope", 15), ("out_of_scope", 15), ("vague_in_scope", 5)):
        assert sum(item.group == group for item in train) == expected
        assert sum(item.group == group for item in test) == expected


def test_calibration_theta_selection_uses_train_only():
    def scored(label: int, score: float, item_id: str) -> ScoredItem:
        return ScoredItem(GateItem(item_id, "q", label, "group"), score)

    train = [
        scored(1, 0.90, "p1"),
        scored(1, 0.70, "p2"),
        scored(0, 0.20, "n1"),
        scored(0, 0.10, "n2"),
    ]
    heldout = [
        scored(1, 0.80, "hp"),
        scored(0, 0.30, "hn"),
    ]

    theta, train_accuracy = select_theta(train, [0.30, 0.50, 0.80])
    # 0.30 and 0.50 tie on train; the documented tie-break picks the value
    # closer to 0.50. The held-out split is still perfect.
    assert theta == 0.50
    assert train_accuracy == 1.0
    assert accuracy(heldout, theta) == 1.0
    assert confusion(train, theta) == {"tp": 2, "tn": 2, "fp": 0, "fn": 0}


def test_premise_trap_file_has_ten_valid_traps():
    payload = json.loads((ROOT / "evals" / "premise_traps.json").read_text(encoding="utf-8"))
    traps = payload["traps"]

    assert len(traps) == 10
    assert len({trap["id"] for trap in traps}) == 10
    allowed = set(payload["expected_behaviour_values"])
    assert {trap["expected_behaviour"] for trap in traps} <= allowed
    assert all(trap["query"].strip() for trap in traps)


def test_premise_runner_stub_validates_without_network():
    from scripts.check_premise_traps import load_traps

    traps = load_traps(ROOT / "evals" / "premise_traps.json")
    assert len(traps) == 10
