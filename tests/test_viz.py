"""ECharts config builder tests: NaN handling, JSON validity, HTML output."""

import json

import numpy as np

from domains.hk.viz import EChartsConfigManager


def test_line_cleans_nan_and_none_to_null():
    manager = EChartsConfigManager()
    index = manager.line("t", ["a", "b", "c"], {"PM2.5": [1.0, float("nan"), None]})

    assert index == 0
    assert manager.charts[0]["option"]["series"][0]["data"] == [1.0, None, None]
    assert manager.charts[0]["option"]["xAxis"]["data"] == ["a", "b", "c"]


def test_bar_flips_series_type():
    manager = EChartsConfigManager()
    index = manager.bar("t", [1, 2], {"s": [1, 2]})

    assert manager.charts[index]["option"]["series"][0]["type"] == "bar"


def test_numpy_scalars_are_serialisable():
    manager = EChartsConfigManager()
    manager.line("t", [1], {"s": np.array([np.float64(2.5), np.int64(3)])})

    assert manager.charts[0]["option"]["series"][0]["data"] == [2.5, 3.0]


def test_to_json_is_strict_json():
    manager = EChartsConfigManager()
    manager.line("t", [1, 2, 3], {"s": [float("inf"), float("-inf"), float("nan")]})

    payload = manager.to_json()
    assert "NaN" not in payload
    assert "Infinity" not in payload
    assert json.loads(payload)[0]["option"]["series"][0]["data"] == [None, None, None]


def test_save_html_embeds_each_chart(tmp_path):
    manager = EChartsConfigManager()
    manager.line("first", ["a"], {"s": [1]})
    manager.bar("second", ["b"], {"s": [2]})

    target = manager.save_html(tmp_path / "charts" / "day4.html")
    html = target.read_text(encoding="utf-8")

    assert "echarts.init" in html
    assert 'id="chart0"' in html
    assert 'id="chart1"' in html
    assert '"text": "first"' in html
    assert '"type": "bar"' in html
    assert len(manager) == 2
