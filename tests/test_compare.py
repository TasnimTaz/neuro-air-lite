"""Unit tests for the saved-artifact comparison helpers."""

from __future__ import annotations

from evals.compare_arms import _bootstrap, _mcnemar_exact, _score_item
from evals.hk_baseline_v2 import CASES
from evals.run_baseline import _summary


def test_mcnemar_exact_matches_the_three_discordant_example():
    # Three losses and no wins is the old multi-vs-single pattern: p=0.25.
    assert abs(_mcnemar_exact(0, 3) - 0.25) < 1e-12
    assert _mcnemar_exact(0, 0) == 1.0


def test_bootstrap_of_zero_differences_is_zero():
    result = _bootstrap([0.0, 0.0, 0.0], iterations=100)
    assert result["mean"] == 0.0
    assert result["ci_low"] == 0.0
    assert result["ci_high"] == 0.0


def test_compare_rescores_a_saved_case_with_the_selected_scorer():
    item = {
        "id": 3,
        "answer": None,
        "answer_pre_verify": None,
        "final_text": "No CO reading available.",
        "final_text_pre_verify": "No CO reading available.",
        "ok": True,
        "pass": False,
        "pass_pre_verify": False,
    }
    assert _score_item(item, "v1") is False
    assert _score_item(item, "v2") is True
    assert _score_item(item, "v2", pre_verify=True) is True


def test_summary_counts_verifier_corrections_and_regressions():
    case = CASES[0]
    runs = [
        {
            "repeat": 1,
            "cases": [
                {
                    "id": case.id,
                    "category": case.category,
                    "difficulty": case.difficulty,
                    "holdout": case.holdout,
                    "pass": False,
                    "pass_pre_verify": True,
                    "pass_v1": False,
                    "pass_v2": False,
                    "semantic_pass": False,
                    "first_attempt_ok": True,
                    "steps": 3,
                    "calls": 4,
                    "tokens": 100,
                    "seconds": 1.0,
                    "stall_count": 0,
                    "verifier_changed_answer": True,
                }
            ],
        }
    ]
    summary = _summary(runs, 1, (case,))
    audit = summary["verifier_audit"]
    assert audit["right_to_wrong"] == 1
    assert audit["wrong_to_right"] == 0
    assert audit["changed_answer"] == 1
