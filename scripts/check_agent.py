"""Day 3 acceptance gate: one live agent, ten hand-verified questions.

    uv run python scripts/check_agent.py --cross-check
    uv run python scripts/check_agent.py --cross-check --only 8
    uv run python scripts/check_agent.py --question "Mean PM2.5 at Tai Po on 2025-11-25?"

The plan's Done-when for Day 3 is behavioural: a single agent must turn a
natural-language question into SQL, run it against the read-only database and
report the right number. Unit tests cannot prove that, so this script runs a
live LLM and checks three things before the questions:

  1. `DB_URL_ADMIN` is not in `os.environ`. The agent loader exports only
     `DB_URL_RO` and `LLM_*`; the admin DSN is handed to the truth queries
     directly, never exported.
  2. Model-written code cannot reach it through pandas (`pd.io.common.os`).
  3. The exact engine the agent receives cannot INSERT, even with the read-only
     transaction default lifted: the GRANT layer answers SQLSTATE 42501.

Each question has a ground-truth SQL query executed on the admin engine. The
answer is read back from the runtime namespace (`ANSWER`), not scraped from
prose, so the comparison is deterministic; the number still has to come from
executed code, which is the property the paper cares about.

Exit code is non-zero if a safety check, a truth query or a case fails.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# `python scripts/foo.py` puts scripts/ on sys.path, not the repo root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from core.agent import run_worker  # noqa: E402
from core.env import admin_url, load_agent_env  # noqa: E402
from core.llm import USAGE, llm, reset_usage  # noqa: E402
from core.prompts import get_system_prompt  # noqa: E402
from domains.hk.db import make_admin_engine  # noqa: E402
from domains.hk.tools import build_runtime  # noqa: E402


@dataclass(frozen=True)
class Case:
    label: str
    question: str
    truth_sql: str
    kind: str = "float"  # float | int


CASES: tuple[Case, ...] = (
    Case(
        "latest-day PM2.5 (Tai Po)",
        "What is the mean PM2.5 concentration in ug/m3 at the station named 'Tai Po' "
        "on the most recent date for which the database has data? Treat that day as "
        "00:00 to 23:59 local time and round to 2 decimals.",
        "WITH last_day AS (SELECT max(datetime)::date AS d FROM hourly_station_air_quality) "
        "SELECT round(avg(a.pm2_5)::numeric, 2) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id, last_day "
        "WHERE s.name_en = 'Tai Po' "
        "AND a.datetime >= last_day.d AND a.datetime < last_day.d + 1",
    ),
    Case(
        "max NO2 Nov 2025 (Causeway Bay)",
        "What was the maximum hourly NO2 concentration in ug/m3 at the station named "
        "'Causeway Bay' during November 2025 (2025-11-01 00:00 to 2025-11-30 23:59)? "
        "Round to 2 decimals.",
        "SELECT round(max(a.no2)::numeric, 2) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Causeway Bay' "
        "AND a.datetime >= '2025-11-01' AND a.datetime < '2025-12-01'",
    ),
    Case(
        "mean AQHI June 2025 (Central)",
        "What was the mean AQHI at the station named 'Central' during June 2025? "
        "Round to 2 decimals.",
        "SELECT round(avg(a.aqhi)::numeric, 2) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Central' "
        "AND a.datetime >= '2025-06-01' AND a.datetime < '2025-07-01'",
    ),
    Case(
        "hours PM2.5 > 50 in 2025 (Yuen Long)",
        "How many hourly readings at the station named 'Yuen Long' in 2025 had PM2.5 "
        "strictly above 50 ug/m3? Answer with a whole number.",
        "SELECT count(*) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Yuen Long' AND a.pm2_5 > 50 "
        "AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'",
        "int",
    ),
    Case(
        "mean wind speed Nov 2025 (Tai Po)",
        "What was the mean wind speed in m/s at the weather station 'wx_taipo' during "
        "November 2025? Round to 2 decimals.",
        "SELECT round(avg(w.wind_speed)::numeric, 2) FROM hourly_station_weather w "
        "JOIN weather_station s ON s.id = w.station_id "
        "WHERE s.station_code = 'wx_taipo' "
        "AND w.datetime >= '2025-11-01' AND w.datetime < '2025-12-01'",
    ),
    Case(
        "max wind gust 2025 (Tap Mun)",
        "What was the maximum hourly wind gust in m/s recorded at the weather station "
        "'wx_tapmun' in 2025? Round to 2 decimals.",
        "SELECT round(max(w.wind_gust)::numeric, 2) FROM hourly_station_weather w "
        "JOIN weather_station s ON s.id = w.station_id "
        "WHERE s.station_code = 'wx_tapmun' "
        "AND w.datetime >= '2025-01-01' AND w.datetime < '2026-01-01'",
    ),
    Case(
        "mean temperature Aug 2025 (Sha Tin)",
        "What was the mean temperature in degrees Celsius at 'wx_shatin' during "
        "August 2025? Round to 2 decimals.",
        "SELECT round(avg(w.temperature)::numeric, 2) FROM hourly_station_weather w "
        "JOIN weather_station s ON s.id = w.station_id "
        "WHERE s.station_code = 'wx_shatin' "
        "AND w.datetime >= '2025-08-01' AND w.datetime < '2025-09-01'",
    ),
    Case(
        "schools within 1500 m (Tai Po)",
        "How many rows in sensitive_area with category 'EDUCATIONAL_INSTITUTION' lie "
        "within 1500 metres of the air quality station named 'Tai Po'? Distances must "
        "be computed in metres. Answer with a whole number.",
        "SELECT count(*) FROM sensitive_area sa, air_quality_station s "
        "WHERE s.name_en = 'Tai Po' AND sa.category = 'EDUCATIONAL_INSTITUTION' "
        "AND ST_DWithin(sa.geometry::geography, s.coordinate, 1500)",
        "int",
    ),
    Case(
        "mean O3 Oct 2025 (Tung Chung)",
        "What was the mean O3 concentration in ug/m3 at the station named 'Tung Chung' "
        "during October 2025? Round to 2 decimals.",
        "SELECT round(avg(a.o3)::numeric, 2) FROM hourly_station_air_quality a "
        "JOIN air_quality_station s ON s.id = a.station_id "
        "WHERE s.name_en = 'Tung Chung' "
        "AND a.datetime >= '2025-10-01' AND a.datetime < '2025-11-01'",
    ),
    Case(
        "roadside station count",
        "How many air quality stations have station_type 'ROADSIDE'? Answer with a "
        "whole number.",
        "SELECT count(*) FROM air_quality_station WHERE station_type = 'ROADSIDE'",
        "int",
    ),
)

ANSWER_HINT = (
    "\n\nWhen you have the final answer, assign the numeric value to a variable named "
    "ANSWER (e.g. `ANSWER = 12.34`) and print it with `print(ANSWER)`. Then reply with "
    "one short sentence that includes the value and no code blocks."
)

SECRET_READ = "print(pd.io.common.os.environ.get('DB_URL_ADMIN'))"
RO_INSERT = "INSERT INTO air_quality_station (station_code) VALUES ('ro_write_probe')"


def _pgcode(error: BaseException) -> str | None:
    return getattr(getattr(error, "orig", None), "pgcode", None)


def safety_checks() -> bool:
    """Prove the isolation claims before spending minutes on the LLM."""
    passed = True

    def report(ok: bool, message: str) -> None:
        nonlocal passed
        print(f"[{'ok' if ok else 'FAIL'}] {message}")
        passed = passed and ok

    report("DB_URL_ADMIN" not in os.environ, "admin DSN absent from os.environ")
    report("POSTGRES_PASSWORD" not in os.environ, "Postgres password absent from os.environ")
    report(bool(os.environ.get("DB_URL_RO")), "read-only DSN present in os.environ")

    runtime = build_runtime()
    try:
        result = runtime.execute(SECRET_READ)
        blocked = (not result.ok) and "SecurityError" in result.error
        report(blocked, "model code cannot read os.environ through pandas")

        try:
            with runtime.ns["engine"].connect() as conn:
                conn.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
                conn.commit()
                try:
                    conn.exec_driver_sql(RO_INSERT)
                except Exception as error:  # noqa: BLE001
                    code = _pgcode(error)
                    report(code == "42501", f"neuro_ro INSERT rejected by grants (SQLSTATE {code})")
                else:
                    report(False, "neuro_ro accepted an INSERT once read-only was lifted")
                finally:
                    conn.rollback()
        except OperationalError as error:
            report(False, f"database unreachable: {error.orig}")
    finally:
        runtime.close()

    return passed


def truth(engine, case: Case) -> float:
    with engine.connect() as conn:
        value = conn.execute(text(case.truth_sql)).scalar_one()
    return float(value)


def agrees(case: Case, answer, expected: float) -> bool:
    if answer is None:
        return False
    try:
        got = float(answer)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(got):
        return False
    if case.kind == "int":
        return abs(got - expected) < 0.5
    return abs(got - expected) <= max(0.05, 0.005 * abs(expected))


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

        try:
            expected = truth(admin, case)
        except Exception as error:  # noqa: BLE001
            return {
                "case": case,
                "expected": None,
                "answer": answer,
                "pass": False,
                "steps": result.steps,
                "executions": result.executions,
                "first_attempt_ok": result.first_attempt_ok,
                "seconds": elapsed,
                "tokens": result.prompt_tokens + result.completion_tokens,
                "ok": result.ok,
                "note": f"truth query failed: {error}",
                "trace": trace,
                "final_text": result.final_text,
            }

        passed = result.ok and agrees(case, answer, expected)
        return {
            "case": case,
            "expected": expected,
            "answer": answer,
            "pass": passed,
            "steps": result.steps,
            "executions": result.executions,
            "first_attempt_ok": result.first_attempt_ok,
            "seconds": elapsed,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "ok": result.ok,
            "note": "",
            "trace": trace,
            "final_text": result.final_text,
        }
    finally:
        runtime.close()


async def run_question(question: str, max_steps: int) -> int:
    runtime = build_runtime()
    try:
        system = get_system_prompt(engine=runtime.ns["engine"])
        print(f"QUESTION: {question}\n")
        result = await run_worker(
            llm=llm,
            runtime=runtime,
            system=system,
            task=question,
            max_steps=max_steps,
            emit=lambda kind, payload: (
                print(f"[{kind}]"),
                print("  " + str(payload).replace("\n", "\n  ")[:4000]),
            ),
        )
        print("\n-- final --")
        print(result.final_text)
        print(
            f"\nsteps={result.steps} executions={result.executions} ok={result.ok} "
            f"first_attempt_ok={result.first_attempt_ok} seconds={result.total_seconds:.1f}"
        )
        print(
            f"tokens prompt={result.prompt_tokens:,} completion={result.completion_tokens:,} "
            f"calls={USAGE['calls']}"
        )
        return 0 if result.ok else 1
    finally:
        runtime.close()


async def main_async(args) -> int:
    # Read the admin DSN before scrubbing, then keep it in this local variable
    # only. Everything below runs with an agent-shaped environment.
    dsn = admin_url() if not args.question else None
    load_agent_env(override=False, scrub=True)

    if args.question:
        if not safety_checks():
            return 1
        return await run_question(args.question, args.max_steps)

    selected = CASES
    if args.only:
        selected = tuple(case for index, case in enumerate(CASES, start=1) if index in args.only)
        if not selected:
            print(f"--only needs an index between 1 and {len(CASES)}", file=sys.stderr)
            return 2

    if not safety_checks():
        print("\nSafety checks failed; refusing to run the LLM suite.", file=sys.stderr)
        return 1

    admin = make_admin_engine(dsn)
    results = []
    try:
        for index, case in enumerate(selected, start=1):
            print(f"\n[{index}/{len(selected)}] {case.label}")
            print(f"      {case.question}")
            reset_usage()
            try:
                outcome = await run_case(case, admin, args.max_steps, args.show_trace)
            except Exception as error:  # noqa: BLE001
                print(f"      ERROR: {type(error).__name__}: {error}")
                print("      Is Ollama running? Start it with: ollama serve")
                return 1
            results.append(outcome)

            status = "PASS" if outcome["pass"] else "FAIL"
            expected = outcome["expected"]
            if expected is None:
                expected_s = "n/a"
            elif case.kind == "int":
                expected_s = f"{expected:.0f}"
            else:
                expected_s = f"{expected:.2f}"
            first = {True: "yes", False: "no", None: "-"}[outcome["first_attempt_ok"]]
            print(
                f"      truth={expected_s}  answer={outcome['answer']!r}  {status}  "
                f"steps={outcome['steps']}  first={first}  {outcome['seconds']:.1f}s  "
                f"tokens={outcome['tokens']:,}"
            )
            if not outcome["pass"]:
                if outcome["note"]:
                    print(f"      note: {outcome['note']}")
                if not args.show_trace:
                    for kind, payload in outcome["trace"][-4:]:
                        print(f"      last [{kind}]:")
                        print("        " + str(payload).replace("\n", "\n        ")[:1200])
                print(f"      final text: {outcome['final_text'][:600]}")
    finally:
        admin.dispose()

    passed = sum(1 for item in results if item["pass"])
    first_attempt = sum(1 for item in results if item["first_attempt_ok"] is True)
    mean_steps = sum(item["steps"] for item in results) / max(len(results), 1)
    mean_seconds = sum(item["seconds"] for item in results) / max(len(results), 1)
    tokens = sum(item["tokens"] for item in results)
    print(
        f"\nRESULT: {passed}/{len(results)} passed | first-attempt {first_attempt}/{len(results)} | "
        f"mean steps {mean_steps:.1f} | mean {mean_seconds:.1f}s | tokens {tokens:,}"
    )
    return 0 if passed == len(results) else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", help="run the agent once on this question")
    parser.add_argument("--cross-check", action="store_true", help="run the built-in suite")
    parser.add_argument("--only", type=int, nargs="*", help="1-based case indices")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--show-trace", action="store_true", help="print every code block")
    args = parser.parse_args(argv)

    if not args.question and not args.cross_check:
        parser.error("pass --question ... or --cross-check")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
