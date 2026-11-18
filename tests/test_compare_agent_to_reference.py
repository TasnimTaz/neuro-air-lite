"""Offline tests for the agent-to-reference comparison contract."""

from __future__ import annotations

from scripts.compare_agent_to_reference import _field_match, _judge_charts


def test_field_match_uses_stated_tolerances_and_exact_rank() -> None:
    assert _field_match("no2_at_2000", 123.24, 123.2)[0] is True
    assert _field_match("no2_at_2000", 123.4, 123.2)[0] is False
    assert _field_match("pm25_rank_of_nearest", 1, 1)[0] is True
    assert _field_match("pm25_rank_of_nearest", 1.5, 1)[0] is False
    assert _field_match("pm25_rank_of_nearest", True, 1)[0] is False


def _chart(
    title: str,
    kind: str,
    x_length: int,
    y_name: str,
    series_length: int,
) -> dict[str, object]:
    return {
        "title": title,
        "option": {
            "xAxis": {"data": [str(index) for index in range(x_length)]},
            "yAxis": {"name": y_name},
            "series": [
                {
                    "name": "series",
                    "type": kind,
                    "data": list(range(series_length)),
                }
            ],
        },
    }


def test_chart_contract_accepts_two_expected_configs() -> None:
    judgement = _judge_charts(
        [
            _chart(
                "Tai Po NO2 at 20:00 vs preceding 14 days",
                "line",
                14,
                "NO2 (ug/m3)",
                14,
            ),
            _chart(
                "PM2.5 evening rise by station",
                "bar",
                18,
                "PM2.5 rise (ug/m3)",
                18,
            ),
        ]
    )
    assert judgement["line_chart_ok"] is True
    assert judgement["bar_chart_ok"] is True


def test_chart_contract_rejects_wrong_lengths() -> None:
    judgement = _judge_charts(
        [
            _chart(
                "Tai Po NO2 at 20:00 vs preceding 14 days",
                "line",
                13,
                "NO2 (ug/m3)",
                13,
            ),
            _chart(
                "PM2.5 evening rise by station",
                "bar",
                17,
                "PM2.5 rise (ug/m3)",
                17,
            ),
        ]
    )
    assert judgement["line_chart_ok"] is False
    assert judgement["bar_chart_ok"] is False
