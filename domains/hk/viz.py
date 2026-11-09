"""ECharts option builders for the Hong Kong agent.

The agent never renders a chart itself; it builds an ECharts `option` dict that
the API/UI (later days) renders. Keeping the builder host-side means the model
only has to pass labels and series, not JSON boilerplate.

`_clean` is the important part: pandas/numpy hand back `NaN` (and occasionally
`None`) for missing hours, and bare `NaN` is not valid JSON. Every non-finite
value becomes `null`, which ECharts renders as a gap instead of failing to parse.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"


class EChartsConfigManager:
    """Accumulate chart configs; each builder returns the chart's index."""

    def __init__(self) -> None:
        self.charts: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _clean(values: Iterable[Any]) -> list[Any]:
        out: list[Any] = []
        for value in values:
            if value is None:
                out.append(None)
                continue
            try:
                if pd.isna(value):
                    out.append(None)
                    continue
            except (TypeError, ValueError):
                pass
            try:
                number = float(value)
            except (TypeError, ValueError):
                out.append(str(value))
                continue
            out.append(number if math.isfinite(number) else None)
        return out

    def _add(self, title: str, option: dict[str, Any]) -> int:
        self.charts.append({"title": title, "option": option})
        return len(self.charts) - 1

    # ------------------------------------------------------------------ builders
    def line(
        self,
        title: str,
        x: Iterable[Any],
        series: Mapping[str, Iterable[Any]],
        y_name: str = "",
    ) -> int:
        """Add a line chart. `series` maps series name -> values."""
        option = {
            "title": {"text": title, "left": "center"},
            "tooltip": {"trigger": "axis"},
            "legend": {"data": list(series), "top": 28},
            "grid": {"left": 60, "right": 30, "top": 70, "bottom": 60},
            "xAxis": {"type": "category", "data": [str(value) for value in x]},
            "yAxis": {"type": "value", "name": y_name},
            "series": [
                {
                    "name": name,
                    "type": "line",
                    "data": self._clean(values),
                    "showSymbol": False,
                    "connectNulls": False,
                }
                for name, values in series.items()
            ],
        }
        return self._add(title, option)

    def bar(
        self,
        title: str,
        x: Iterable[Any],
        series: Mapping[str, Iterable[Any]],
        y_name: str = "",
    ) -> int:
        """Add a bar chart with the same inputs as `line`."""
        index = self.line(title, x, series, y_name)
        for item in self.charts[index]["option"]["series"]:
            item["type"] = "bar"
        return index

    # ------------------------------------------------------------------- output
    def to_json(self, indent: int | None = 2) -> str:
        """Serialise every accumulated chart; safe to embed in an API response."""
        return json.dumps(self.charts, ensure_ascii=False, indent=indent, default=str)

    def save_html(self, path: str | Path) -> Path:
        """Write a standalone page that renders every chart, for manual review."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        blocks: list[str] = []
        for index, chart in enumerate(self.charts):
            payload = json.dumps(chart["option"], ensure_ascii=False, default=str)
            payload = payload.replace("</", "<\\/")
            blocks.append(f'<div id="chart{index}" class="chart"></div>')
            blocks.append(
                "<script>"
                f'echarts.init(document.getElementById("chart{index}")).setOption({payload});'
                "</script>"
            )

        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>neuro-air-lite charts</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 24px; background: #fafafa; }}
  .chart {{ width: 100%; max-width: 1100px; height: 420px; margin: 0 auto 32px; }}
</style>
<script src="{CDN}"></script>
</head>
<body>
{chr(10).join(blocks)}
</body>
</html>
"""
        target.write_text(html, encoding="utf-8")
        return target

    def __len__(self) -> int:
        return len(self.charts)
