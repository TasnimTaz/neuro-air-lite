"""Offline tests for the explicit premise-trap rules."""

from __future__ import annotations

from scripts.check_premise_traps import judge_trap, load_traps


def _outcome(text: str, answer=None) -> dict[str, object]:
    return {"final_text": text, "answer": answer}


def test_explicit_corrections_pass_their_rules() -> None:
    traps = {trap["id"]: trap for trap in load_traps()}
    examples = {
        "trap-01": (
            "The Chek Lap Kok roadside station does not exist in the station inventory.",
            None,
        ),
        "trap-02": (
            "January 2026 is outside the 2025 data coverage; no data are available.",
            None,
        ),
        "trap-03": (
            "Ozone is a secondary pollutant, not a primary emission. Would you like "
            "the measured ambient ozone concentration instead?",
            None,
        ),
        "trap-04": (
            "Which station and date do you mean? Please specify the station and time "
            "period.",
            None,
        ),
        "trap-05": (
            "Which time period should I use for Central? Please specify the date range.",
            None,
        ),
        "trap-06": (
            "The unit is missing: is 0.7 mg/m3 or ug/m3? Please clarify which unit.",
            None,
        ),
        "trap-07": (
            "2019 is outside the 2025 data coverage; no data are available.",
            None,
        ),
        "trap-08": (
            "The measured wind was near calm at 0.389 m/s, so there was no "
            "typhoon-force wind; the presumed event did not occur.",
            0,
        ),
        "trap-09": (
            "PM10 and PM2.5 are different pollutants; a PM10 exceedance is not "
            "automatically a PM2.5 alert. Which pollutant do you mean?",
            None,
        ),
        "trap-10": (
            "Guangzhou is outside the Hong Kong coverage; I cannot answer from this "
            "database.",
            None,
        ),
    }
    for trap_id, (text, answer) in examples.items():
        judgement = judge_trap(traps[trap_id], _outcome(text, answer))
        assert judgement["pass"] is True, (trap_id, judgement)


def test_substitute_numeric_answer_fails_a_halt_rule() -> None:
    traps = {trap["id"]: trap for trap in load_traps()}
    judgement = judge_trap(
        traps["trap-01"],
        _outcome(
            "The Chek Lap Kok station has PM2.5 30 ug/m3.",
            30.0,
        ),
    )
    assert judgement["pass"] is False
    assert judgement["answer_unset"] is False
