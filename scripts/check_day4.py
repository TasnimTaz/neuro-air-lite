"""Day 4 tool/smoke gate: schema prompt, chart tool, geocoder, 15-query v1 smoke.

    uv run python scripts/check_day4.py
    uv run python scripts/check_day4.py --only 3 7 12
    uv run python scripts/check_day4.py --show-trace
    uv run python scripts/check_day4.py --skip-probes

This is the Day 4 implementation check, not the Day 9 reference. `evals/hk_baseline.py`
is **smoke set v1**, and it is contaminated: three of its questions were worked
examples in the old prompt. The frozen reference is `evals/hk_baseline_v2.py`,
run by `evals/run_baseline.py`.

The Day 3 gate proved one agent can answer questions from SQL. Day 4 adds three
capabilities the paper's analysts need and the plan requires:

  1. a schema prompt that carries units, provenance, AQHI/AQO context and worked
     examples (the prompt is built from `domains/hk/schema_prompt.py`);
  2. a host-side `geocode()` tool (Nominatim, Lands Department ALS fallback);
  3. a host-side `charts` tool that turns query results into ECharts configs.

Static capability probes check the tools without the LLM. Two live probes then
check that the model actually calls them. The 15-question v1 smoke run keeps the
plan's Day 4 Done-when: at least 10 must pass. Ground truth is recomputed from
the database and compared with the pinned values in `evals/hk_baseline.py`; a
mismatch is printed and saved in the artifact rather than silently accepted.

Artifacts: `runs/day4_baseline.json` and `runs/day4_chart.html` (both gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# `python scripts/foo.py` puts scripts/ on sys.path, not the repo root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from core.agent import run_worker  # noqa: E402
from core.env import admin_url, load_agent_env  # noqa: E402
from core.llm import llm, reset_usage  # noqa: E402
from domains.hk.db import make_admin_engine  # noqa: E402
from domains.hk.schema_prompt import get_system_prompt  # noqa: E402
from domains.hk.tools import HK_BOUNDS, build_runtime, geocode  # noqa: E402
from domains.hk.viz import EChartsConfigManager  # noqa: E402
from evals.hk_baseline import ANSWER_HINT, CASES, Case  # noqa: E402

PASS_THRESHOLD = 10
ARTIFACT = ROOT / "runs" / "day4_baseline.json"
CHART_HTML = ROOT / "runs" / "day4_chart.html"
PROBE_HTML = ROOT / "runs" / "day4_chart_probe.html"

CHART_QUESTION = (
    "Using the Tai Po air quality station, build a line chart of the daily mean "
    "PM2.5 concentration for November 2025 with charts.line(...). Put the date "
    "labels on the x-axis and one series named 'PM2.5 (ug/m3)'. Then set ANSWER "
    "to the number of days plotted and print it."
)
CHART_TRUTH = 30.0

GEO_QUESTION = (
    "Geocode the address 'Tai Po, Hong Kong' with geocode(), find the nearest air "
    "quality station to those coordinates, and report that station's 2025 annual "
    "mean PM2.5 in ug/m3. Set ANSWER to the value and round to 2 decimals."
)
GEO_TRUTH = 15.26


def agrees(kind: str, answer, expected: float) -> bool:
    if answer is None:
        return False
    try:
        got = float(answer)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(got):
        return False
    if kind == "int":
        return abs(got - expected) < 0.5
    return abs(got - expected) <= max(0.05, 0.005 * abs(expected))


def same_as_pinned(kind: str, computed: float, pinned: float) -> bool:
    if kind == "int":
        return abs(computed - pinned) < 0.5
    return abs(computed - pinned) <= max(0.01, 0.001 * abs(pinned))


def truth(engine, case: Case) -> float:
    with engine.connect() as conn:
        return float(conn.execute(text(case.truth_sql)).scalar_one())


def _trace_notes(trace: list[tuple[str, object]]) -> tuple[str, str]:
    last_code = ""
    last_error = ""
    for kind, payload in trace:
        text_payload = str(payload)
        if kind == "code":
            last_code = text_payload
        elif kind == "output" and "ERROR:" in text_payload:
            last_error = text_payload
    return last_code, last_error


# --------------------------------------------------------------------- probes


def capability_probes() -> tuple[dict[str, dict], bool]:
    """Exercise geocode and charts without the LLM."""
    results: dict[str, dict] = {}
    passed = True

    def report(name: str, ok: bool, detail: str) -> None:
        nonlocal passed
        results[name] = {"pass": bool(ok), "detail": detail}
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
        passed = passed and bool(ok)

    # geocode: a real Hong Kong address resolves inside the bbox.
    try:
        hit = geocode("Tai Po, Hong Kong")
        lat_min, lat_max, lon_min, lon_max = HK_BOUNDS
        inside = lat_min <= hit["latitude"] <= lat_max and lon_min <= hit["longitude"] <= lon_max
        report(
            "geocode_hit",
            inside and hit["source"] in {"nominatim", "als"},
            f"{hit['source']} {hit['latitude']:.4f}, {hit['longitude']:.4f}",
        )
    except Exception as error:  # noqa: BLE001
        report("geocode_hit", False, f"{type(error).__name__}: {error}")

    # geocode: a miss is a clear ValueError, not an IndexError or a silent 0,0.
    # ALS is fuzzy and returns a candidate for most junk strings, so the miss
    # probe uses a string that produces no candidates at all.
    try:
        geocode("asdfghjkl qwerty")
    except ValueError as error:
        report("geocode_miss", True, str(error))
    except Exception as error:  # noqa: BLE001
        report("geocode_miss", False, f"{type(error).__name__}: {error}")
    else:
        report("geocode_miss", False, "no exception for an impossible address")

    # charts: NaN becomes null, numpy scalar is serialisable, JSON is strict.
    manager = EChartsConfigManager()
    index = manager.line(
        "probe",
        ["a", "b", "c"],
        {"PM2.5": [1.0, float("nan"), None]},
        "ug/m3",
    )
    data = manager.charts[index]["option"]["series"][0]["data"]
    bar_index = manager.bar("probe bar", ["a", "b"], {"NO2": [3, 4]})
    bar_type = manager.charts[bar_index]["option"]["series"][0]["type"]
    try:
        payload = manager.to_json()
        strict = "NaN" not in payload and "Infinity" not in payload and json.loads(payload)
        target = manager.save_html(PROBE_HTML)
        html_ok = target.exists() and "echarts.init" in target.read_text(encoding="utf-8")
    except Exception as error:  # noqa: BLE001
        strict, html_ok = False, False
        report("charts", False, f"{type(error).__name__}: {error}")
    else:
        report(
            "charts",
            data == [1.0, None, None] and bar_type == "bar" and bool(strict) and html_ok,
            f"cleaned={data}, bar={bar_type}, html={html_ok}",
        )

    # prompt: assembled, carries the knowledge base, stays inside the 16k context.
    try:
        prompt = get_system_prompt()
        size_ok = 8000 < len(prompt) < 24000
        content_ok = (
            "<knowledge_base>" in prompt
            and "37.5" in prompt
            and "9789240034228" in prompt
            and "geocode(address)" in prompt
            and "charts.line(" in prompt
        )
        report(
            "prompt",
            size_ok and content_ok,
            f"{len(prompt):,} chars (~{len(prompt) // 4:,} tokens), "
            f"knowledge_base={content_ok}",
        )
    except Exception as error:  # noqa: BLE001
        report("prompt", False, f"{type(error).__name__}: {error}")

    return results, passed


# ------------------------------------------------------------------------ cases


async def run_case(case: Case, admin, max_steps: int, show_trace: bool) -> dict:
    runtime = build_runtime()
    try:
        system = get_system_prompt(engine=runtime.ns["engine"])
        trace: list[tuple[str, object]] = []

        def emit(kind: str, payload) -> None:
            trace.append((kind, payload))
            if show_trace:
                print(f"    [{kind}]")
                print("      " + str(payload).replace("\n", "\n      ")[:2000])

        started = time.perf_counter()
        result = await run_worker(
            llm=llm,
            runtime=runtime,
            system=system,
            task=case.question + ANSWER_HINT,
            max_steps=max_steps,
            emit=emit,
        )
        elapsed = time.perf_counter() - started
        answer = runtime.ns.get("ANSWER")

        note = ""
        try:
            expected = truth(admin, case)
        except Exception as error:  # noqa: BLE001
            expected = case.expected
            note = f"truth query failed ({error}); using pinned value"
        else:
            if not same_as_pinned(case.kind, expected, case.expected):
                note = (
                    f"PINNED VALUE MISMATCH: eval pins {case.expected}, "
                    f"database now returns {expected}"
                )

        last_code, last_error = _trace_notes(trace)
        return {
            "id": case.id,
            "category": case.category,
            "label": case.label,
            "question": case.question,
            "pinned": case.expected,
            "expected": expected,
            "answer": answer,
            "pass": bool(result.ok and agrees(case.kind, answer, expected)),
            "steps": result.steps,
            "executions": result.executions,
            "first_attempt_ok": result.first_attempt_ok,
            "seconds": elapsed,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "ok": result.ok,
            "note": note,
            "last_code": last_code,
            "last_error": last_error,
            "final_text": result.final_text,
        }
    finally:
        runtime.close()


async def probe_live_chart(max_steps: int, show_trace: bool) -> dict:
    runtime = build_runtime()
    try:
        system = get_system_prompt(engine=runtime.ns["engine"])

        def emit(kind: str, payload) -> None:
            if show_trace:
                print(f"    [{kind}]")
                print("      " + str(payload).replace("\n", "\n      ")[:2000])

        result = await run_worker(
            llm=llm,
            runtime=runtime,
            system=system,
            task=CHART_QUESTION + ANSWER_HINT,
            max_steps=max_steps,
            emit=emit,
        )
        answer = runtime.ns.get("ANSWER")
        manager = runtime.ns["charts"]
        checks = {"answer_days": agrees("int", answer, CHART_TRUTH)}
        if manager.charts:
            option = manager.charts[0]["option"]
            x = option["xAxis"]["data"]
            series = option["series"]
            checks["chart_built"] = True
            checks["x_has_30_days"] = len(x) == int(CHART_TRUTH)
            checks["series_present"] = bool(series) and len(series[0]["data"]) == len(x)
            checks["pm25_series"] = any("PM2.5" in item["name"] for item in series)
            manager.save_html(CHART_HTML)
        else:
            checks["chart_built"] = False
        return {
            "name": "live_chart",
            "question": CHART_QUESTION,
            "expected": CHART_TRUTH,
            "answer": answer,
            "checks": checks,
            "pass": bool(result.ok and all(checks.values())),
            "steps": result.steps,
            "seconds": result.total_seconds,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "final_text": result.final_text,
            "artifact": str(CHART_HTML) if manager.charts else None,
        }
    finally:
        runtime.close()


async def probe_live_geocode(max_steps: int, show_trace: bool) -> dict:
    runtime = build_runtime()
    calls: list[str] = []
    injected = runtime.ns["geocode"]

    def counted(address: str) -> dict:
        calls.append(address)
        return injected(address)

    runtime.ns["geocode"] = counted
    try:
        system = get_system_prompt(engine=runtime.ns["engine"])

        def emit(kind: str, payload) -> None:
            if show_trace:
                print(f"    [{kind}]")
                print("      " + str(payload).replace("\n", "\n      ")[:2000])

        result = await run_worker(
            llm=llm,
            runtime=runtime,
            system=system,
            task=GEO_QUESTION + ANSWER_HINT,
            max_steps=max_steps,
            emit=emit,
        )
        answer = runtime.ns.get("ANSWER")
        checks = {
            "tool_called": bool(calls),
            "answer_matches": agrees("float", answer, GEO_TRUTH),
        }
        return {
            "name": "live_geocode",
            "question": GEO_QUESTION,
            "expected": GEO_TRUTH,
            "answer": answer,
            "geocode_calls": calls,
            "checks": checks,
            "pass": bool(result.ok and all(checks.values())),
            "steps": result.steps,
            "seconds": result.total_seconds,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "final_text": result.final_text,
        }
    finally:
        runtime.close()


# ------------------------------------------------------------------------- main


async def main_async(args) -> int:
    # Read the admin DSN before scrubbing, then keep it in this local variable.
    dsn = admin_url()
    load_agent_env(override=False, scrub=True)

    print(f"model: {os.environ.get('LLM_MODEL', '?')} at {os.environ.get('LLM_BASE_URL', '?')}")
    preview = get_system_prompt()
    print(f"prompt: {len(preview):,} chars (~{len(preview) // 4:,} tokens)")
    if "DB_URL_ADMIN" in os.environ:
        print("[FAIL] DB_URL_ADMIN is exported; the agent environment is not scrubbed", file=sys.stderr)
        return 1
    print("[ok] DB_URL_ADMIN absent from the agent environment")

    if args.skip_probes:
        probes, probes_ok = {}, True
    else:
        probes, probes_ok = capability_probes()

    selected = CASES
    if args.only:
        selected = tuple(case for case in CASES if case.id in args.only)
        if not selected:
            print(f"--only needs ids between 1 and {len(CASES)}", file=sys.stderr)
            return 2

    admin = make_admin_engine(dsn)
    results: list[dict] = []
    try:
        for index, case in enumerate(selected, start=1):
            print(f"\n[{index}/{len(selected)}] #{case.id} {case.label} ({case.category})")
            print(f"      {case.question}")
            reset_usage()
            try:
                outcome = await run_case(case, admin, args.max_steps, args.show_trace)
            except OperationalError as error:
                print(f"      database error: {error.orig}", file=sys.stderr)
                return 1
            except Exception as error:  # noqa: BLE001
                print(f"      ERROR: {type(error).__name__}: {error}", file=sys.stderr)
                print("      Is Ollama running? Start it with: ollama serve", file=sys.stderr)
                return 1
            results.append(outcome)

            status = "PASS" if outcome["pass"] else "FAIL"
            expected = outcome["expected"]
            expected_s = f"{expected:.0f}" if case.kind == "int" else f"{expected:.2f}"
            first = {True: "yes", False: "no", None: "-"}[outcome["first_attempt_ok"]]
            print(
                f"      truth={expected_s}  answer={outcome['answer']!r}  {status}  "
                f"steps={outcome['steps']}  first={first}  {outcome['seconds']:.1f}s  "
                f"tokens={outcome['tokens']:,}"
            )
            if outcome["note"]:
                print(f"      note: {outcome['note']}")
            if not outcome["pass"]:
                if outcome["last_error"]:
                    print("      last error:")
                    print("        " + outcome["last_error"].replace("\n", "\n        ")[:1200])
                if not args.show_trace and outcome["last_code"]:
                    print("      last code:")
                    print("        " + outcome["last_code"].replace("\n", "\n        ")[:1200])
                print(f"      final text: {outcome['final_text'][:600]}")
    finally:
        admin.dispose()

    live: list[dict] = []
    live_ok = True
    if not args.only and not args.skip_probes:
        for probe in (probe_live_chart, probe_live_geocode):
            print(f"\n[live] {probe.__name__}")
            reset_usage()
            try:
                outcome = await probe(args.max_steps, args.show_trace)
            except Exception as error:  # noqa: BLE001
                outcome = {
                    "name": probe.__name__,
                    "pass": False,
                    "checks": {"exception": f"{type(error).__name__}: {error}"},
                    "expected": None,
                    "answer": None,
                    "seconds": 0.0,
                    "tokens": 0,
                    "final_text": "",
                }
            live.append(outcome)
            live_ok = live_ok and outcome["pass"]
            expected_text = (
                f"{outcome['expected']:.2f}" if outcome["expected"] is not None else "n/a"
            )
            print(
                f"      truth={expected_text}  answer={outcome['answer']!r}  "
                f"{'PASS' if outcome['pass'] else 'FAIL'}  checks={outcome['checks']}  "
                f"{outcome['seconds']:.1f}s  tokens={outcome['tokens']:,}"
            )
            if not outcome["pass"]:
                print(f"      final text: {outcome['final_text'][:600]}")

    passed = sum(1 for item in results if item["pass"])
    first_attempt = sum(1 for item in results if item["first_attempt_ok"] is True)
    mean_steps = sum(item["steps"] for item in results) / max(len(results), 1)
    mean_seconds = sum(item["seconds"] for item in results) / max(len(results), 1)
    tokens = sum(item["tokens"] for item in results)

    by_category: dict[str, str] = {}
    for case in selected:
        items = [item for item in results if item["category"] == case.category]
        good = sum(1 for item in items if item["pass"])
        by_category[case.category] = f"{good}/{len(items)}"

    print(
        f"\nRESULT: {passed}/{len(results)} passed | first-attempt {first_attempt}/{len(results)} | "
        f"mean steps {mean_steps:.1f} | mean {mean_seconds:.1f}s | tokens {tokens:,}"
    )
    print("BY CATEGORY: " + " | ".join(f"{name} {value}" for name, value in by_category.items()))
    if not args.only and not args.skip_probes:
        print("LIVE PROBES: " + " | ".join(
            f"{item['name']} {'PASS' if item['pass'] else 'FAIL'}" for item in live
        ))

    success = (
        passed >= PASS_THRESHOLD
        and (args.skip_probes or probes_ok)
        and (args.skip_probes or live_ok)
    )

    if not args.no_save:
        payload = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": os.environ.get("LLM_MODEL"),
            "base_url": os.environ.get("LLM_BASE_URL"),
            "prompt_chars": len(get_system_prompt()),
            "threshold": PASS_THRESHOLD,
            "capability_probes": probes,
            "capability_probes_pass": probes_ok,
            "cases": [
                {
                    **item,
                    "last_code": item["last_code"][:2000],
                    "last_error": item["last_error"][:2000],
                }
                for item in results
            ],
            "live_probes": live,
            "live_probes_pass": live_ok,
            "summary": {
                "passed": passed,
                "total": len(results),
                "first_attempt": first_attempt,
                "mean_steps": mean_steps,
                "mean_seconds": mean_seconds,
                "tokens": tokens,
                "by_category": by_category,
                "success": success,
            },
        }
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"ARTIFACT: {ARTIFACT}")

    if not success:
        print(
            f"\nDay 4 gate not met: need >= {PASS_THRESHOLD}/{len(CASES)} and all probes passing.",
            file=sys.stderr,
        )
    return 0 if success else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", type=int, nargs="*", help=f"1-based case ids (1..{len(CASES)})")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--show-trace", action="store_true", help="print every code block")
    parser.add_argument(
        "--skip-probes",
        action="store_true",
        help="skip all probes (static and live); run only the 15-query baseline",
    )
    parser.add_argument("--no-save", action="store_true", help="do not write the JSON artifact")
    args = parser.parse_args(argv)

    if args.skip_probes:
        print("static and live probes skipped; only the 15-query baseline will run")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
