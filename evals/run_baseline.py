"""Run the single- or multi-agent baseline and write a reproducible JSON artifact.

    uv run python evals/run_baseline.py --validate-only
    uv run python evals/run_baseline.py --repeats 3
    uv run python evals/run_baseline.py --arm multi --repeats 3
    uv run python evals/run_baseline.py --only 3 7 12 --repeats 1
    uv run python evals/run_baseline.py --no-holdout --repeats 1

The default set is `evals/hk_baseline_v2.py`: 20 real-user questions, six marked
as hold-out. The runner never edits the prompt. It records the sha256 of the
assembled prompt and the model name in every JSON, so a later run can prove
whether the reference changed.

The default budget is 20 solver steps because the plan's Day 5/9 matched
comparison is 20 total solver steps for both arms. For multi, the coordinator's
planning call is recorded separately as orchestration overhead; worker and
verifier steps are capped at 20. The committed Day 4 reference used 10 steps;
use `--max-steps 10` only to reproduce that earlier artifact, not for Day 9.

The answer is read from the runtime namespace's `ANSWER` variable, not scraped
from the final text (except for text answers, where the final sentence is also
accepted). Truth is recomputed on the admin engine before scoring; a mismatch
with the pinned value is reported and makes the run invalid rather than silently
moving the benchmark.

Artifacts: `runs/baseline/<timestamp>/run.json` and `prompt.txt` (gitignored);
multi artifacts go to `runs/multi/<timestamp>/` with `multi_prompts.txt`.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# `python evals/run_baseline.py` puts evals/ on sys.path, not the repo root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from core.agent import run_worker  # noqa: E402
from core.env import admin_url, load_agent_env  # noqa: E402
from core.llm import llm, reset_usage  # noqa: E402
from core.orchestrator import run_pipeline  # noqa: E402
from core.prompts import HKT  # noqa: E402
from domains.hk.db import make_admin_engine  # noqa: E402
from domains.hk.roles import COORDINATOR_SYSTEM, MULTI_RULES, ROLE_PROMPTS  # noqa: E402
from domains.hk.schema_prompt import get_system_prompt  # noqa: E402
from domains.hk.tools import build_runtime  # noqa: E402
from evals.hk_baseline_v2 import ANSWER_HINT, CASES, Case  # noqa: E402

PROMPT_TIME = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=HKT)
UNAVAILABLE = ("not available", "no data", "unavailable", "missing", "null")
CANNOT_ANSWER = (
    "cannot",
    "can't",
    "not possible",
    "does not exist",
    "no such",
    "outside",
    "not available",
    "no data",
)


def _multi_prompts_text() -> str:
    """Everything multi-only that is sent to the model, for reproducible hashes."""
    return "\n\n".join(
        [
            "COORDINATOR:\n" + COORDINATOR_SYSTEM,
            "SHARED RULES:\n" + MULTI_RULES,
            *(
                f"ROLE {role}:\n{prompt}"
                for role, prompt in ROLE_PROMPTS.items()
            ),
        ]
    )


def _normalise_text(value: Any) -> str:
    return " ".join(str(value).strip().lower().split())


def _is_zero(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value)) and abs(float(value)) < 1e-9
    except (TypeError, ValueError):
        return False


def _same_as_pinned(case: Case, computed: Any) -> bool:
    if case.kind in {"float", "int"}:
        if computed is None:
            return False
        try:
            got = float(computed)
            want = float(case.expected)
        except (TypeError, ValueError):
            return False
        if case.kind == "int":
            return abs(got - want) < 0.5
        return abs(got - want) <= max(0.01, 0.001 * abs(want))
    if case.kind == "text":
        if case.expected is None:
            return computed is None
        return computed is not None and _normalise_text(computed) == _normalise_text(case.expected)
    if case.kind == "unanswerable":
        return computed is None or _is_zero(computed)
    return False


def _truth_value(admin, case: Case) -> Any:
    with admin.connect() as conn:
        row = conn.execute(text(case.truth_sql)).fetchone()
    if row is None:
        return None
    return row[0]


def _score(case: Case, answer: Any, final_text: str, result_ok: bool) -> tuple[bool, str]:
    """Return (pass, reason). Never use the pinned value to score."""
    if not result_ok:
        return False, "agent run did not complete"

    text = _normalise_text(final_text)

    if case.kind == "float":
        if answer is None:
            return False, "ANSWER is unset"
        try:
            got = float(answer)
        except (TypeError, ValueError):
            return False, f"ANSWER is not numeric: {answer!r}"
        if not math.isfinite(got):
            return False, f"ANSWER is not finite: {answer!r}"
        tolerance = case.tolerance or max(0.05, 0.005 * abs(float(case.expected)))
        ok = abs(got - float(case.expected)) <= tolerance
        return ok, f"abs({got} - {case.expected}) <= {tolerance}"

    if case.kind == "int":
        if answer is None:
            return False, "ANSWER is unset"
        try:
            got = float(answer)
        except (TypeError, ValueError):
            return False, f"ANSWER is not numeric: {answer!r}"
        ok = abs(got - float(case.expected)) < 0.5
        return ok, f"abs({got} - {case.expected}) < 0.5"

    if case.kind == "text":
        if case.expected is None:
            if _is_zero(answer):
                return False, "answered 0 for a missing value"
            answer_text = _normalise_text(answer) if answer is not None else ""
            if not any(term in text or term in answer_text for term in UNAVAILABLE):
                return False, "did not say the value is unavailable"
            if answer is not None and not isinstance(answer, str) and not _is_zero(answer):
                return False, f"unexpected numeric ANSWER for a missing value: {answer!r}"
            return True, "reported unavailable"
        expected = _normalise_text(case.expected)
        answer_text = _normalise_text(answer) if isinstance(answer, str) else ""
        ok = expected in answer_text or expected in text
        return ok, f"expected text {case.expected!r}"

    if case.kind == "unanswerable":
        if answer is not None:
            return False, f"ANSWER should be unset, got {answer!r}"
        ok = any(phrase in text for phrase in CANNOT_ANSWER)
        return ok, "said the question cannot be answered"

    return False, f"unknown kind {case.kind!r}"


def _trace_parts(trace: list[tuple[str, object]]) -> tuple[list[str], list[str], str]:
    code_blocks: list[str] = []
    outputs: list[str] = []
    last_error = ""
    for kind, payload in trace:
        rendered = str(payload)
        if kind == "code":
            code_blocks.append(rendered)
        elif kind == "output":
            outputs.append(rendered)
            if "ERROR:" in rendered:
                last_error = rendered
        elif kind == "assistant":
            outputs.append(rendered)
    return code_blocks, outputs, last_error


async def run_case(
    case: Case,
    admin,
    max_steps: int,
    show_trace: bool,
    *,
    arm: str = "single",
    verify_reserve: int = 6,
    synth_reserve: int = 3,
) -> dict:
    runtime = build_runtime()
    trace: list[tuple[str, object]] = []
    try:
        system = get_system_prompt(engine=runtime.ns["engine"], now=PROMPT_TIME)

        def emit(kind: str, payload) -> None:
            trace.append((kind, payload))
            if show_trace:
                print(f"    [{kind}]")
                print("      " + str(payload).replace("\n", "\n      ")[:2000])

        started = time.perf_counter()
        pipeline = None
        if arm == "multi":
            pipeline = await run_pipeline(
                llm=llm,
                runtime=runtime,
                role_prompts=ROLE_PROMPTS,
                shared_rules=system + "\n\n" + MULTI_RULES,
                coordinator_system=COORDINATOR_SYSTEM,
                query=case.question,
                task_suffix=ANSWER_HINT,
                max_total=max_steps,
                verify_reserve=verify_reserve,
                synth_reserve=synth_reserve,
                emit=emit,
            )
            final_text = pipeline.final_text
            run_ok = pipeline.ok
            steps = pipeline.solver_steps
            executions = sum(run.executions for run in pipeline.worker_runs)
            first_attempt_ok = next(
                (
                    run.first_attempt_ok
                    for run in pipeline.worker_runs
                    if run.executions > 0
                ),
                None,
            )
            tokens = pipeline.total_tokens
            calls = pipeline.total_calls
        else:
            result = await run_worker(
                llm=llm,
                runtime=runtime,
                system=system,
                task=case.question + ANSWER_HINT,
                max_steps=max_steps,
                emit=emit,
            )
            final_text = result.final_text
            run_ok = result.ok
            steps = result.steps
            executions = result.executions
            first_attempt_ok = result.first_attempt_ok
            tokens = result.prompt_tokens + result.completion_tokens
            calls = result.steps
        elapsed = time.perf_counter() - started
        answer = runtime.ns.get("ANSWER")

        expected = None
        truth_note = ""
        pinned_ok = True
        try:
            expected = _truth_value(admin, case)
            pinned_ok = _same_as_pinned(case, expected)
            if not pinned_ok:
                truth_note = (
                    f"PINNED VALUE MISMATCH: eval pins {case.expected!r}, "
                    f"database now returns {expected!r}"
                )
        except Exception as error:  # noqa: BLE001
            pinned_ok = False
            truth_note = f"truth query failed: {type(error).__name__}: {error}"

        passed, reason = _score(case, answer, final_text, run_ok)
        code_blocks, outputs, last_error = _trace_parts(trace)
        outcome = {
            "id": case.id,
            "category": case.category,
            "difficulty": case.difficulty,
            "holdout": case.holdout,
            "question": case.question,
            "kind": case.kind,
            "tolerance": case.tolerance,
            "pinned": case.expected,
            "expected": expected,
            "pinned_ok": pinned_ok,
            "note": " | ".join(part for part in (case.note, truth_note) if part),
            "answer": answer,
            "pass": passed,
            "reason": reason,
            "steps": steps,
            "calls": calls,
            "executions": executions,
            "first_attempt_ok": first_attempt_ok,
            "seconds": elapsed,
            "tokens": tokens,
            "ok": run_ok,
            "last_error": last_error[:4000],
            "code_blocks": code_blocks[-6:],
            "outputs": outputs[-12:],
            "final_text": final_text,
        }
        if pipeline is not None:
            outcome["pipeline"] = pipeline.as_dict()
            outcome["plan"] = pipeline.plan
            outcome["plan_source"] = pipeline.plan_source
            outcome["plan_error"] = pipeline.plan_error
            outcome["worker_runs"] = [run.as_dict() for run in pipeline.worker_runs]
            outcome["verifier_text"] = pipeline.verifier_text
        return outcome
    finally:
        runtime.close()


def _summary(runs: list[dict], repeats: int, cases: tuple[Case, ...]) -> dict:
    per_case: list[dict] = []
    for case in cases:
        items = [item for run in runs for item in run["cases"] if item["id"] == case.id]
        passed = sum(item["pass"] for item in items)
        per_case.append(
            {
                "id": case.id,
                "category": case.category,
                "difficulty": case.difficulty,
                "holdout": case.holdout,
                "pass": passed,
                "n": len(items),
                "rate": passed / len(items) if items else 0.0,
                "first_attempt": sum(item["first_attempt_ok"] is True for item in items),
                "steps": sum(item["steps"] for item in items) / max(len(items), 1),
                "calls": sum(item.get("calls", item["steps"]) for item in items)
                / max(len(items), 1),
                "tokens": sum(item["tokens"] for item in items),
                "seconds": sum(item["seconds"] for item in items),
            }
        )

    def group_rate(predicate) -> float:
        items = [item for run in runs for item in run["cases"] if predicate(item)]
        return sum(item["pass"] for item in items) / max(len(items), 1)

    repeat_rates = [
        sum(item["pass"] for item in run["cases"]) / max(len(run["cases"]), 1)
        for run in runs
    ]
    all_items = [item for run in runs for item in run["cases"]]
    holdout_items = [item for item in all_items if item["holdout"]]
    non_holdout_items = [item for item in all_items if not item["holdout"]]

    by_category = {
        category: group_rate(lambda item, c=category: item["category"] == c)
        for category in sorted({case.category for case in cases})
    }
    by_difficulty = {
        difficulty: group_rate(lambda item, d=difficulty: item["difficulty"] == d)
        for difficulty in sorted({case.difficulty for case in cases})
    }

    return {
        "per_case": per_case,
        "overall": {
            "mean_pass_rate": sum(repeat_rates) / max(len(repeat_rates), 1),
            "min_pass_rate": min(repeat_rates) if repeat_rates else 0.0,
            "max_pass_rate": max(repeat_rates) if repeat_rates else 0.0,
            "passed": sum(item["pass"] for item in all_items),
            "total": len(all_items),
            "first_attempt": sum(item["first_attempt_ok"] is True for item in all_items),
            "mean_steps": sum(item["steps"] for item in all_items) / max(len(all_items), 1),
            "mean_calls": sum(item.get("calls", item["steps"]) for item in all_items)
            / max(len(all_items), 1),
            "calls": sum(item.get("calls", item["steps"]) for item in all_items),
            "mean_seconds": sum(item["seconds"] for item in all_items) / max(len(all_items), 1),
            "tokens": sum(item["tokens"] for item in all_items),
        },
        "holdout": {
            "passed": sum(item["pass"] for item in holdout_items),
            "total": len(holdout_items),
            "rate": sum(item["pass"] for item in holdout_items) / max(len(holdout_items), 1),
        },
        "non_holdout": {
            "passed": sum(item["pass"] for item in non_holdout_items),
            "total": len(non_holdout_items),
            "rate": sum(item["pass"] for item in non_holdout_items)
            / max(len(non_holdout_items), 1),
        },
        "by_category": by_category,
        "by_difficulty": by_difficulty,
        "repeats": repeats,
    }


def _print_summary(summary: dict) -> None:
    print("\nPER-CASE (pass count / repeats, mean steps, tokens, seconds)")
    print(f"{'id':>3}  {'category':<12} {'diff':<7} {'hold':<4} {'pass':>7} {'steps':>6} {'tokens':>9} {'sec':>7}")
    for item in summary["per_case"]:
        print(
            f"{item['id']:>3}  {item['category']:<12} {item['difficulty']:<7} "
            f"{'yes' if item['holdout'] else 'no':<4} "
            f"{item['pass']:>3}/{item['n']:<3} {item['steps']:>6.1f} "
            f"{item['tokens']:>9,} {item['seconds']:>7.1f}"
        )

    overall = summary["overall"]
    print(
        f"\nOVERALL: mean pass {overall['mean_pass_rate']:.1%} "
        f"(range {overall['min_pass_rate']:.1%}-{overall['max_pass_rate']:.1%} over "
        f"{summary['repeats']} repeats; {overall['passed']}/{overall['total']} case-runs) | "
        f"first-attempt {overall['first_attempt']}/{overall['total']} | "
        f"mean {overall['mean_steps']:.1f} steps / {overall['mean_calls']:.1f} calls, "
        f"{overall['mean_seconds']:.1f}s, {overall['tokens']:,} tokens"
    )
    print(
        "HOLDOUT: "
        f"{summary['holdout']['passed']}/{summary['holdout']['total']} "
        f"({summary['holdout']['rate']:.1%}) | "
        "NON-HOLDOUT: "
        f"{summary['non_holdout']['passed']}/{summary['non_holdout']['total']} "
        f"({summary['non_holdout']['rate']:.1%})"
    )
    print("BY CATEGORY: " + " | ".join(f"{k} {v:.1%}" for k, v in summary["by_category"].items()))
    print("BY DIFFICULTY: " + " | ".join(f"{k} {v:.1%}" for k, v in summary["by_difficulty"].items()))


async def main_async(args) -> int:
    # Long runs are launched with their output redirected to a log file. Line
    # buffering makes that log useful while the run is still in progress.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:  # pragma: no cover - older/embedded streams
            pass

    # Read the admin DSN before scrubbing, then keep it in this local variable.
    dsn = admin_url()
    load_agent_env(override=False, scrub=True)
    if args.model:
        os.environ["LLM_MODEL"] = args.model

    if args.arm not in {"single", "multi"}:
        print(f"--arm must be 'single' or 'multi', got {args.arm!r}", file=sys.stderr)
        return 2
    if "DB_URL_ADMIN" in os.environ:
        print("[FAIL] DB_URL_ADMIN is exported; the agent environment is not scrubbed", file=sys.stderr)
        return 1

    selected = tuple(
        case
        for case in CASES
        if (not args.only or case.id in args.only) and (not args.no_holdout or not case.holdout)
    )
    if not selected:
        print("no cases selected", file=sys.stderr)
        return 2

    admin = make_admin_engine(dsn)
    preview = ""
    try:
        # Validate every pinned value before spending LLM time.
        validation: dict[int, dict] = {}
        for case in selected:
            try:
                value = _truth_value(admin, case)
                ok = _same_as_pinned(case, value)
            except Exception as error:  # noqa: BLE001
                value, ok = None, False
                print(f"[FAIL] #{case.id} truth query: {type(error).__name__}: {error}", file=sys.stderr)
            validation[case.id] = {"expected": value, "pinned_ok": ok, "pinned": case.expected}
            if not ok:
                print(
                    f"[FAIL] #{case.id} pinned={case.expected!r} database={value!r}",
                    file=sys.stderr,
                )
        if not all(item["pinned_ok"] for item in validation.values()):
            print("\nTruth validation failed; fix the pinned values before running.", file=sys.stderr)
            return 1
        print(f"[ok] {len(selected)} truth values match their pinned values")

        if args.validate_only:
            return 0

        preview = get_system_prompt(engine=admin, now=PROMPT_TIME)
        prompt_sha256 = hashlib.sha256(preview.encode("utf-8")).hexdigest()
        multi_prompts = _multi_prompts_text()
        multi_prompts_sha256 = hashlib.sha256(multi_prompts.encode("utf-8")).hexdigest()
        print(
            f"model: {os.environ.get('LLM_MODEL', '?')} at {os.environ.get('LLM_BASE_URL', '?')}\n"
            f"prompt: {len(preview):,} chars (~{len(preview) // 4:,} tokens), "
            f"sha256={prompt_sha256}"
        )
        if args.arm == "multi":
            print(
                f"multi prompts: {len(multi_prompts):,} chars, sha256={multi_prompts_sha256}\n"
                f"budget: {args.max_steps} solver steps + 1 planner call | "
                f"verify reserve={args.verify_reserve}, synth reserve={args.synth_reserve}"
            )
        print(f"repeats: {args.repeats} | max steps: {args.max_steps} | cases: {len(selected)}")

        runs: list[dict] = []
        for repeat in range(1, args.repeats + 1):
            print(f"\n=== repeat {repeat}/{args.repeats} ===")
            run_cases: list[dict] = []
            for index, case in enumerate(selected, start=1):
                print(f"\n[{index}/{len(selected)}] #{case.id} {case.question}")
                reset_usage()
                try:
                    outcome = await run_case(
                        case,
                        admin,
                        args.max_steps,
                        args.show_trace,
                        arm=args.arm,
                        verify_reserve=args.verify_reserve,
                        synth_reserve=args.synth_reserve,
                    )
                except OperationalError as error:
                    print(f"      database error: {error.orig}", file=sys.stderr)
                    return 1
                except Exception as error:  # noqa: BLE001
                    print(f"      ERROR: {type(error).__name__}: {error}", file=sys.stderr)
                    print("      Is Ollama running? Start it with: ollama serve", file=sys.stderr)
                    return 1
                outcome["repeat"] = repeat
                run_cases.append(outcome)
                first = {True: "yes", False: "no", None: "-"}[outcome["first_attempt_ok"]]
                print(
                    f"      {'PASS' if outcome['pass'] else 'FAIL'}  "
                    f"expected={outcome['expected']!r} answer={outcome['answer']!r}  "
                    f"steps={outcome['steps']} calls={outcome['calls']} first={first} "
                    f"{outcome['seconds']:.1f}s tokens={outcome['tokens']:,}  "
                    f"({outcome['reason']})"
                )
                if outcome["note"]:
                    print(f"      note: {outcome['note']}")
                if not outcome["pass"]:
                    if outcome["last_error"]:
                        print("      last error: " + outcome["last_error"].replace("\n", "\n        ")[:1200])
                    print(f"      final text: {outcome['final_text'][:600]}")
            run_passed = sum(item["pass"] for item in run_cases)
            runs.append(
                {
                    "repeat": repeat,
                    "passed": run_passed,
                    "total": len(run_cases),
                    "cases": run_cases,
                }
            )
            print(f"\nrepeat {repeat}: {run_passed}/{len(run_cases)} passed")
    finally:
        admin.dispose()

    summary = _summary(runs, args.repeats, selected)
    _print_summary(summary)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    arm_dir = "baseline" if args.arm == "single" else "multi"
    out_dir = Path(args.out) if args.out else ROOT / "runs" / arm_dir / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompt.txt").write_text(preview, encoding="utf-8")
    if args.arm == "multi":
        (out_dir / "multi_prompts.txt").write_text(multi_prompts, encoding="utf-8")
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "arm": args.arm,
        "model": os.environ.get("LLM_MODEL"),
        "base_url": os.environ.get("LLM_BASE_URL"),
        "seed_label": args.seed_label,
        "prompt_chars": len(preview),
        "prompt_sha256": prompt_sha256,
        "multi_prompts_sha256": multi_prompts_sha256,
        "planner_policy": "planner_call_separate_from_20_solver_steps",
        "verify_reserve": args.verify_reserve,
        "synth_reserve": args.synth_reserve,
        "prompt_time": PROMPT_TIME.isoformat(),
        "repeats": args.repeats,
        "max_steps": args.max_steps,
        "selected": [case.id for case in selected],
        "validation": validation,
        "summary": summary,
        "runs": runs,
    }
    artifact = out_dir / "run.json"
    artifact.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nARTIFACT: {artifact}")
    print(f"PROMPT:   {out_dir / 'prompt.txt'}")
    if args.arm == "multi":
        print(f"MULTI:    {out_dir / 'multi_prompts.txt'}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("single", "multi"), default="single")
    parser.add_argument("--model", default=None, help="override LLM_MODEL for this run")
    parser.add_argument("--repeats", type=int, default=3, help="number of repeats (default 3)")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=20,
        help="per-run solver-step budget (default 20; Day 5/9 matched comparison; use 10 only for the Day 4 reference)",
    )
    parser.add_argument(
        "--verify-reserve",
        type=int,
        default=6,
        help="multi verifier reserve within the solver budget (default 6)",
    )
    parser.add_argument(
        "--synth-reserve",
        type=int,
        default=3,
        help="multi synthesis reserve within the plan budget (default 3)",
    )
    parser.add_argument("--only", type=int, nargs="*", help="subset of case ids")
    parser.add_argument("--no-holdout", action="store_true", help="skip the six hold-out cases")
    parser.add_argument("--seed-label", default=None, help="free-form label stored in the JSON")
    parser.add_argument("--out", default=None, help="output directory (default runs/baseline/<timestamp>)")
    parser.add_argument("--validate-only", action="store_true", help="check truth SQL and exit")
    parser.add_argument("--show-trace", action="store_true")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
