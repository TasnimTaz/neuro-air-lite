"""Compare the live single agent with the deterministic Tai Po reference.

This is a separate, non-frozen replication check. It does not change the
evaluated prompt, the scorer, or any baseline artifact. It asks the single arm
(v3 prompt, no workers, no verifier) for the paper's Tai Po reanalysis fields
under an exact ``ANSWER`` dictionary contract, three times, and compares every
field with the reference computed from the read-only database.

The reference definitions are the paper's Supplementary Note 3 definitions used
by ``analysis/tai_po_reference.py``:

* matched-hour baseline: the same 20:00 hour on 2025-11-12..2025-11-25, sample
  standard deviation (``ddof=1``);
* network contrast: event evening 2025-11-26 18:00-23:59 against the same
  clock-hours on those 14 days;
* nearest-station excess: Tai Po's PM2.5 rise minus the mean rise of the other
  17 stations.

The script also records the two requested chart configs. Chart checks are
structural (type, title, x length, series count, y-axis name), not visual
judgements. Failures are printed verbatim.

Usage::

    uv run python scripts/compare_agent_to_reference.py \
        --out runs/reference_compare/day8_single_3x

Artifacts are gitignored under ``runs/``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.tai_po_reference import compute_reference  # noqa: E402
from core.agent import BUDGET_EXHAUSTED_SENTINEL, run_worker  # noqa: E402
from core.env import load_agent_env  # noqa: E402
from core.llm import llm  # noqa: E402
from core.orchestrator import _stall_guarded_llm  # noqa: E402
from domains.hk.db import make_ro_engine  # noqa: E402
from domains.hk.schema_prompt import get_system_prompt  # noqa: E402
from domains.hk.tools import build_runtime  # noqa: E402
from evals.run_baseline import (  # noqa: E402
    PROMPT_TIME,
    _single_system_prompt,
    _trace_parts,
    _write_json,
)

FIELDS = (
    "no2_at_2000",
    "pm25_post_peak",
    "matched_hour_z",
    "matched_hour_max_prev_14d",
    "network_mean_rise",
    "nearest_station_rise_vs_network",
    "pm25_rank_of_nearest",
    "wind_speed_2000",
)

TOLERANCES: dict[str, float | None] = {
    "no2_at_2000": 0.1,
    "pm25_post_peak": 0.1,
    "matched_hour_z": 0.1,
    "matched_hour_max_prev_14d": 0.1,
    "network_mean_rise": 0.2,
    "nearest_station_rise_vs_network": 0.2,
    "pm25_rank_of_nearest": None,  # exact integer
    "wind_speed_2000": 0.01,
}

REFERENCE_HINT = """

Return ``ANSWER`` as one Python dict with exactly these keys:
``no2_at_2000``, ``pm25_post_peak``, ``matched_hour_z``,
``matched_hour_max_prev_14d``, ``network_mean_rise``,
``nearest_station_rise_vs_network``, ``pm25_rank_of_nearest``,
``wind_speed_2000``. Use numeric values (round to 4 decimals), and use an
integer rank. Do not put prose inside the dict. Also build exactly these two
charts with the supplied ``charts`` manager:

1. ``charts.line("Tai Po NO2 at 20:00 vs preceding 14 days", x=<the 14 dates>,
   series={"Tai Po NO2": <the 14 NO2 values>}, y_name="NO2 (ug/m3)")``
2. ``charts.bar("PM2.5 evening rise by station", x=<station codes>,
   series={"PM2.5 rise": <each station's event-evening PM2.5 rise>},
   y_name="PM2.5 rise (ug/m3)")``

Then print the dict and reply with one short sentence and no code block.
"""


QUESTION = """Reproduce the Tai Po fire reanalysis for 2025-11-26 from the read-only
database. Return one ANSWER dict with exactly the eight keys named in the
contract, using these fixed definitions:

- no2_at_2000: Tai Po NO2 at 2025-11-26 20:00, ug/m3.
- pm25_post_peak: maximum hourly Tai Po PM2.5 in [2025-11-26 15:00,
  2025-11-27 15:00), ug/m3.
- matched_hour_z: (Tai Po NO2 at 2025-11-26 20:00 minus the mean of Tai Po NO2
  at 20:00 on each of 2025-11-12 through 2025-11-25) divided by the sample
  standard deviation (ddof=1) of those 14 values.
- matched_hour_max_prev_14d: maximum of those 14 matched 20:00 NO2 values,
  ug/m3.
- network_mean_rise: mean over the other 17 stations of (station mean NO2 over
  the event evening 2025-11-26 18:00-23:59 minus station mean NO2 over the
  same clock-hours on 2025-11-12 through 2025-11-25), ug/m3.
- nearest_station_rise_vs_network: Tai Po mean PM2.5 over the event evening
  minus the mean over the other 17 stations of (station event-evening mean
  PM2.5 minus station same-clock-hour baseline mean PM2.5), ug/m3.
- pm25_rank_of_nearest: rank of Tai Po's PM2.5 event-evening rise among all 18
  stations, where 1 is the largest rise. Integer.
- wind_speed_2000: wind speed at wx_taipo at 2025-11-26 20:00, m/s.

Do not substitute a different window. The database contains Hong Kong stations
and hourly data for calendar year 2025.
"""


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _field_match(key: str, value: Any, expected: Any) -> tuple[bool, str]:
    if isinstance(value, bool):
        return False, f"expected a number, got boolean {value!r}"
    if key == "pm25_rank_of_nearest":
        try:
            number = float(value)
            if not number.is_integer():
                return False, f"expected integer {expected!r}, got {value!r}"
            return int(number) == int(expected), ""
        except (TypeError, ValueError):
            return False, f"expected integer {expected!r}, got {value!r}"
    observed = _number(value)
    target = _number(expected)
    if observed is None or target is None:
        return False, f"expected numeric {expected!r}, got {value!r}"
    tolerance = TOLERANCES[key]
    if tolerance is None:
        return observed == target, ""
    ok = abs(observed - target) <= tolerance
    return ok, "" if ok else f"|{observed} - {target}| > {tolerance}"


def _chart_summary(charts: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for chart in charts:
        option = chart.get("option") or {}
        series = option.get("series") or []
        x_axis = option.get("xAxis") or {}
        y_axis = option.get("yAxis") or {}
        items.append(
            {
                "title": chart.get("title") or (option.get("title") or {}).get("text"),
                "types": [item.get("type") for item in series],
                "series_names": [item.get("name") for item in series],
                "series_lengths": [len(item.get("data") or []) for item in series],
                "x_length": len(x_axis.get("data") or []),
                "y_name": y_axis.get("name"),
            }
        )
    return {"count": len(items), "charts": items}


def _judge_charts(charts: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _chart_summary(charts)
    items = summary["charts"]
    line = next(
        (
            item
            for item in items
            if item["types"] == ["line"]
            and "NO2" in str(item["title"]).upper()
        ),
        None,
    )
    bar = next(
        (
            item
            for item in items
            if item["types"] == ["bar"]
            and "PM2.5" in str(item["title"]).upper()
        ),
        None,
    )
    line_ok = bool(
        line
        and line["x_length"] == 14
        and len(line["series_lengths"]) == 1
        and line["series_lengths"][0] == 14
        and "ug/m3" in str(line["y_name"]).lower()
    )
    bar_ok = bool(
        bar
        and bar["x_length"] == 18
        and len(bar["series_lengths"]) == 1
        and bar["series_lengths"][0] == 18
        and "ug/m3" in str(bar["y_name"]).lower()
    )
    return {
        "line_chart_ok": line_ok,
        "bar_chart_ok": bar_ok,
        "line_chart": line,
        "bar_chart": bar,
        "summary": summary,
    }


async def run_once(
    repeat: int,
    *,
    expected: dict[str, Any],
    max_steps: int,
    show_trace: bool,
) -> dict[str, Any]:
    runtime = build_runtime()
    trace: list[tuple[str, object]] = []

    def emit(kind: str, payload: object) -> None:
        trace.append((kind, payload))
        if show_trace:
            print(f"  [{kind}] {str(payload)[:1600]}")

    try:
        base = get_system_prompt(engine=runtime.ns["engine"], now=PROMPT_TIME)
        system = _single_system_prompt(base, True)
        started = time.perf_counter()
        result = await run_worker(
            llm=_stall_guarded_llm(llm, "solver", emit),
            runtime=runtime,
            system=system,
            task=QUESTION + REFERENCE_HINT,
            max_steps=max_steps,
            emit=emit,
        )
        seconds = time.perf_counter() - started
        answer = runtime.ns.get("ANSWER")
        charts = list(runtime.ns["charts"].charts)
        final_text = result.final_text.strip()
        ok = bool(final_text) and final_text != BUDGET_EXHAUSTED_SENTINEL
        code_blocks, outputs, last_error = _trace_parts(trace)
        fields: dict[str, Any] = {}
        answer_dict = answer if isinstance(answer, dict) else {}
        for key in FIELDS:
            value = answer_dict.get(key)
            matched, detail = _field_match(key, value, expected.get(key))
            fields[key] = {
                "value": value,
                "expected": expected.get(key),
                "tolerance": TOLERANCES[key],
                "pass": matched,
                "detail": detail,
            }
        return {
            "repeat": repeat,
            "question": QUESTION,
            "answer": answer,
            "answer_is_dict": isinstance(answer, dict),
            "fields": fields,
            "field_passes": sum(item["pass"] for item in fields.values()),
            "field_total": len(fields),
            "charts": _judge_charts(charts),
            "steps": result.steps,
            "calls": result.steps,
            "executions": result.executions,
            "first_attempt_ok": result.first_attempt_ok,
            "seconds": seconds,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "ok": ok,
            "final_text": final_text,
            "last_error": last_error[:4000],
            "code_blocks": code_blocks[-6:],
            "outputs": outputs[-12:],
        }
    finally:
        runtime.close()


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {
        key: {
            "passes": sum(run["fields"][key]["pass"] for run in runs),
            "total": len(runs),
            "failures": [
                {
                    "repeat": run["repeat"],
                    "value": run["fields"][key]["value"],
                    "expected": run["fields"][key]["expected"],
                    "detail": run["fields"][key]["detail"],
                }
                for run in runs
                if not run["fields"][key]["pass"]
            ],
        }
        for key in FIELDS
    }
    return {
        "fields": fields,
        "field_matches": sum(item["passes"] for item in fields.values()),
        "field_checks": len(FIELDS) * len(runs),
        "answer_dict_repeats": sum(run["answer_is_dict"] for run in runs),
        "line_chart_passes": sum(run["charts"]["line_chart_ok"] for run in runs),
        "bar_chart_passes": sum(run["charts"]["bar_chart_ok"] for run in runs),
        "chart_repeats": len(runs),
        "run_ok_repeats": sum(run["ok"] for run in runs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "runs" / "reference_compare" / "day8_single_3x",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--show-trace", action="store_true")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")

    load_agent_env(override=False, scrub=True)
    if args.model:
        os.environ["LLM_MODEL"] = args.model
    if args.base_url:
        os.environ["LLM_BASE_URL"] = args.base_url

    reference_engine = make_ro_engine()
    try:
        reference = compute_reference(reference_engine)
    finally:
        reference_engine.dispose()
    expected = {key: reference["observed"].get(key) for key in FIELDS}
    if not reference.get("ok"):
        print("[FAIL] the deterministic reference is degraded", file=sys.stderr)
        return 1

    prompt_runtime = build_runtime()
    try:
        base_prompt = get_system_prompt(
            engine=prompt_runtime.ns["engine"], now=PROMPT_TIME
        )
    finally:
        prompt_runtime.close()
    base_sha256 = hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()
    single_prompt_sha256 = hashlib.sha256(
        _single_system_prompt(base_prompt, True).encode("utf-8")
    ).hexdigest()

    args.out.mkdir(parents=True, exist_ok=True)
    _write_json(args.out / "reference.json", reference)
    _write_json(
        args.out / "contract.json",
        {
            "fields": list(FIELDS),
            "tolerances": TOLERANCES,
            "expected": expected,
            "question": QUESTION,
            "hint": REFERENCE_HINT,
            "chart_contract": {
                "line": {
                    "type": "line",
                    "title_contains": "NO2",
                    "x_length": 14,
                    "series_count": 1,
                    "series_length": 14,
                    "y_name_contains": "ug/m3",
                },
                "bar": {
                    "type": "bar",
                    "title_contains": "PM2.5",
                    "x_length": 18,
                    "series_count": 1,
                    "series_length": 18,
                    "y_name_contains": "ug/m3",
                },
            },
        },
    )
    print(f"reference ok={reference['ok']} fields={len(FIELDS)}")
    print(f"model: {os.environ.get('LLM_MODEL')} at {os.environ.get('LLM_BASE_URL')}")
    print(f"repeats: {args.repeats} | max_steps: {args.max_steps}")

    runs: list[dict[str, Any]] = []
    # The OpenAI client is bound to the event loop that created it. Reusing
    # ``asyncio.run`` per repeat closes that loop and makes the next request
    # fail with ``RuntimeError: Event loop is closed``. Keep one loop for all
    # repeats, as the single-verified runner does.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        for repeat in range(1, args.repeats + 1):
            case_path = args.out / "cases" / f"repeat-{repeat:02d}.json"
            if case_path.exists():
                run = json.loads(case_path.read_text(encoding="utf-8"))
                print(f"[resume] repeat {repeat}")
            else:
                print(f"\n[run] repeat {repeat}")
                run = loop.run_until_complete(
                    run_once(
                        repeat,
                        expected=expected,
                        max_steps=args.max_steps,
                        show_trace=args.show_trace,
                    )
                )
                _write_json(case_path, run)
            runs.append(run)
            print(
                f"  fields {run['field_passes']}/{run['field_total']} | "
                f"dict={run['answer_is_dict']} | line={run['charts']['line_chart_ok']} "
                f"bar={run['charts']['bar_chart_ok']} | {run['seconds']:.1f}s"
            )
            for key in FIELDS:
                item = run["fields"][key]
                if not item["pass"]:
                    print(
                        f"    FAIL {key}: value={item['value']!r} "
                        f"expected={item['expected']!r} {item['detail']}"
                    )
            print(f"    final: {run['final_text']}")
    finally:
        loop.close()

    aggregate = _aggregate(runs)
    artifact = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "arm": "single",
        "prompt": "v3 single (base + merged-role addendum)",
        "base_sha256": base_sha256,
        "single_prompt_sha256": single_prompt_sha256,
        # Kept for compatibility with the first live artifact; this is the
        # full single prompt, not the base-only hash used by the frozen runner.
        "prompt_sha256": single_prompt_sha256,
        "model": os.environ.get("LLM_MODEL"),
        "base_url": os.environ.get("LLM_BASE_URL"),
        "repeats": args.repeats,
        "max_steps": args.max_steps,
        "fields": list(FIELDS),
        "tolerances": TOLERANCES,
        "expected": expected,
        "reference_ok": reference["ok"],
        "aggregate": aggregate,
        "runs": runs,
    }
    _write_json(args.out / "run.json", artifact)
    print("\nPER-FIELD MATCHES (passes / repeats)")
    for key in FIELDS:
        item = aggregate["fields"][key]
        print(f"  {key:38s} {item['passes']}/{item['total']}")
    print(
        f"OVERALL fields {aggregate['field_matches']}/{aggregate['field_checks']} | "
        f"ANSWER dict {aggregate['answer_dict_repeats']}/{aggregate['chart_repeats']} | "
        f"line chart {aggregate['line_chart_passes']}/{aggregate['chart_repeats']} | "
        f"bar chart {aggregate['bar_chart_passes']}/{aggregate['chart_repeats']} | "
        f"completed runs {aggregate['run_ok_repeats']}/{aggregate['chart_repeats']}"
    )
    print(f"ARTIFACT: {args.out / 'run.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
