"""Scorer version tests: v1 history, v2 primary, semantic audit view."""

from __future__ import annotations

from evals.hk_baseline_v2 import CASES
from evals.run_baseline import _score

CASE_BY_ID = {case.id: case for case in CASES}


def test_v2_accepts_reading_available_wording_that_v1_rejects():
    case = CASE_BY_ID[3]  # CO is NULL at Sha Tin: text expected None.
    text = "No CO reading available for Sha Tin in 2025."

    v1_pass, _ = _score(case, None, text, True, scorer="v1")
    v2_pass, reason = _score(case, None, text, True, scorer="v2")

    assert v1_pass is False
    assert v2_pass is True
    assert reason == "reported unavailable"


def test_v2_still_fails_a_silent_zero_for_a_missing_value():
    case = CASE_BY_ID[3]
    passed, reason = _score(case, 0, "not available", True, scorer="v2")

    assert passed is False
    assert "0 for a missing value" in reason


def test_v2_accepts_not_in_the_data_for_an_invalid_premise():
    case = CASE_BY_ID[4]  # Kennedy Town does not exist.
    passed, _ = _score(
        case,
        None,
        "That station is not in the data, so the question cannot be answered.",
        True,
        scorer="v2",
    )

    assert passed is True


def test_v2_accepts_could_not_for_an_ambiguous_entity():
    case = CASE_BY_ID[4]
    text = "The station 'Kennedy Town' could not be uniquely identified."
    v1_pass, _ = _score(case, None, text, True, scorer="v1")
    v2_pass, _ = _score(case, None, text, True, scorer="v2")

    assert v1_pass is False
    assert v2_pass is True


def test_unanswerable_still_requires_an_unset_answer():
    case = CASE_BY_ID[4]
    passed, reason = _score(case, 0, "No such station exists.", True, scorer="v2")

    assert passed is False
    assert "ANSWER should be unset" in reason


def test_semantic_view_accepts_a_broader_unavailable_reason():
    case = CASE_BY_ID[16]  # The row exists but PM2.5 is NULL.
    text = "The PM2.5 field is empty in that record; it was not recorded."

    v2_pass, _ = _score(case, None, text, True, scorer="v2")
    semantic_pass, _ = _score(case, None, text, True, scorer="semantic")

    assert v2_pass is True
    assert semantic_pass is True


def test_scorer_does_not_turn_a_wrong_numeric_answer_into_a_pass():
    case = CASE_BY_ID[1]
    passed, reason = _score(case, 21.0, "The altitude is 21 metres.", True, scorer="semantic")

    assert passed is False
    assert "abs(21.0 - 20.0)" in reason
