"""Run the optional-preferred single + verifier control.

This is a non-frozen control arm for the Day 5b review:

    single solver (14 steps) -> the same verifier role/prompt as multi (6 steps)

The total is 20 solver steps, so the single arm is not given the verifier's
budget. The prompt is the v3 single prompt (base prompt plus the merged-role
addendum) used by the v3 single artifact. The verifier is the unchanged
``verification`` role prompt with the same ``MULTI_RULES`` and ``ANSWER_HINT``
as the multi arm. The frozen scorer is reused through ``evals.run_baseline``.

Only the 14 non-hold-out tasks are run, three repeats each. Artifacts are
gitignored under ``runs/single_verified/``.

Usage::

    uv run python scripts/run_single_verified.py --out runs/single_verified/day5b_v3_sv_nonholdout_3x
    uv run python scripts/run_single_verified.py --out ... --only 1 3 4 --repeats 1
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

from core.agent import BUDGET_EXHAUSTED_SENTINEL, run_worker  # noqa: E402
from core.env import admin_url, load_agent_env  # noqa: E402
from core.llm import llm  # noqa: E402
from core.orchestrator import (  # noqa: E402
    _answer_snapshot,
    _answers_equal,
    _stall_guarded_llm,
    _worker_system,
)
from domains.hk.db import make_admin_engine  # noqa: E402
from domains.hk.roles import (  # noqa: E402
    MERGED_ROLE_ADDENDUM,
    MULTI_RULES,
    ROLE_PROMPTS,
)
from domains.hk.schema_prompt import get_system_prompt  # noqa: E402
from domains.hk.tools import build_runtime  # noqa: E402
from evals.hk_baseline_v2 import ANSWER_HINT, CASES, Case  # noqa: E402
from evals.run_baseline import (  # noqa: E402
    DEFAULT_SCORER,
    PROMPT_TIME,
    _case_path,
    _print_summary,
    _same_as_pinned,
    _score,
    _single_system_prompt,
    _summary,
    _trace_parts,
    _truth_value,
    _write_json,
)

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _selected(only: list[int] | None) -> tuple[Case, ...]:
    cases = tuple(
        case
        for case in CASES
        if not case.holdout and (not only or case.id in only)
    )
    if not cases:
        raise SystemExit("no non-hold-out cases selected")
    return cases


async def run_case(
    case: Case,
    admin,
    *,
    solver_steps: int,
    verifier_steps: int,
    show_trace: bool,
) -> dict[str, Any]:
    runtime = build_runtime()
    trace: list[tuple[str, object]] = []

    def emit(kind: str, payload: object) -> None:
        trace.append((kind, payload))
        if show_trace:
            print(f"    [{kind}]")
            print("      " + str(payload).replace("\n", "\n      ")[:2000])

    try:
        base_system = get_system_prompt(engine=runtime.ns["engine"], now=PROMPT_TIME)
        single_system = _single_system_prompt(base_system, True)
        task = case.question + ANSWER_HINT
        started = time.perf_counter()

        solver = await run_worker(
            llm=llm,
            runtime=runtime,
            system=single_system,
            task=task,
            max_steps=solver_steps,
            emit=emit,
        )
        pre_raw = _answer_snapshot(runtime)
        pre_answer = runtime.ns.get("ANSWER")
        pre_text = solver.final_text.strip()
        pre_ok = bool(pre_text) and pre_text != BUDGET_EXHAUSTED_SENTINEL

        verifier_system = _worker_system(
            ROLE_PROMPTS["verification"],
            base_system + "\n\n" + MULTI_RULES,
            runtime,
            task,
            verifier=True,
        )
        verifier = await run_worker(
            llm=_stall_guarded_llm(llm, "verification", emit),
            runtime=runtime,
            system=verifier_system,
            task=task,
            max_steps=verifier_steps,
            emit=emit,
        )
        elapsed = time.perf_counter() - started
        post_raw = _answer_snapshot(runtime)
        answer = runtime.ns.get("ANSWER")
        verifier_text = verifier.final_text.strip()
        final_text = verifier_text or pre_text
        final_ok = bool(final_text) and final_text != BUDGET_EXHAUSTED_SENTINEL
        changed = not _answers_equal(pre_raw, post_raw)
        mismatch = changed or "MISMATCH" in verifier_text.upper()

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

        passed, reason = _score(case, answer, final_text, final_ok, scorer=DEFAULT_SCORER)
        pass_pre, reason_pre = _score(
            case, pre_answer, pre_text, pre_ok, scorer=DEFAULT_SCORER
        )
        pass_v1, reason_v1 = _score(case, answer, final_text, final_ok, scorer="v1")
        pass_v2, reason_v2 = _score(case, answer, final_text, final_ok, scorer="v2")
        semantic_pass, semantic_reason = _score(
            case, answer, final_text, final_ok, scorer="semantic"
        )
        code_blocks, outputs, last_error = _trace_parts(trace)
        steps = solver.steps + verifier.steps
        return {
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
            "answer_pre_verify": pre_answer,
            "pass": passed,
            "reason": reason,
            "pass_pre_verify": pass_pre,
            "reason_pre_verify": reason_pre,
            "pass_v1": pass_v1,
            "reason_v1": reason_v1,
            "pass_v2": pass_v2,
            "reason_v2": reason_v2,
            "semantic_pass": semantic_pass,
            "semantic_reason": semantic_reason,
            "scorer_version": DEFAULT_SCORER,
            "verifier_mode": "overwrite",
            "verifier_changed_answer": changed,
            "verifier_mismatch": mismatch,
            "verifier_text": verifier_text,
            "solver_steps": solver.steps,
            "verifier_steps": verifier.steps,
            "stall_count": 0,
            "steps": steps,
            "calls": steps,
            "executions": solver.executions + verifier.executions,
            "first_attempt_ok": solver.first_attempt_ok,
            "seconds": elapsed,
            "tokens": solver.prompt_tokens
            + solver.completion_tokens
            + verifier.prompt_tokens
            + verifier.completion_tokens,
            "ok": final_ok,
            "last_error": last_error[:4000],
            "code_blocks": code_blocks[-6:],
            "outputs": outputs[-12:],
            "final_text": final_text,
            "final_text_pre_verify": pre_text,
            "repeat": 0,
        }
    finally:
        runtime.close()


def _meta(
    *,
    base_prompt: str,
    verifier_prompt: str,
    selected: tuple[Case, ...],
    repeats: int,
    solver_steps: int,
    verifier_steps: int,
    validation: dict[int, dict],
) -> dict[str, Any]:
    addendum = MERGED_ROLE_ADDENDUM
    return {
        "generated_at": _now_iso(),
        "arm": "single-verified",
        "model": os.environ.get("LLM_MODEL"),
        "base_url": os.environ.get("LLM_BASE_URL"),
        "prompt_chars": len(base_prompt),
        "prompt_sha256": hashlib.sha256(base_prompt.encode("utf-8")).hexdigest(),
        "base_sha256": hashlib.sha256(base_prompt.encode("utf-8")).hexdigest(),
        "single_roles": True,
        "single_addendum_sha256": hashlib.sha256(
            addendum.encode("utf-8")
        ).hexdigest(),
        "verifier_prompt_sha256": hashlib.sha256(
            verifier_prompt.encode("utf-8")
        ).hexdigest(),
        "planner_policy": "none",
        "verify_reserve": verifier_steps,
        "solver_steps": solver_steps,
        "max_steps": solver_steps + verifier_steps,
        "verifier_mode": "overwrite",
        "scorer_version": DEFAULT_SCORER,
        "scorers": ["v1", "v2", "semantic"],
        "prompt_time": PROMPT_TIME.isoformat(),
        "repeats": repeats,
        "selected": [case.id for case in selected],
        "validation": validation,
    }


async def main_async(args: argparse.Namespace) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:
            pass

    dsn = admin_url()
    load_agent_env(override=False, scrub=True)
    if "DB_URL_ADMIN" in os.environ:
        print("[FAIL] DB_URL_ADMIN is exported; agent env is not scrubbed", file=sys.stderr)
        return 1
    if args.model:
        os.environ["LLM_MODEL"] = args.model
    if args.base_url:
        os.environ["LLM_BASE_URL"] = args.base_url

    selected = _selected(args.only)
    admin = make_admin_engine(dsn)
    try:
        validation: dict[int, dict] = {}
        for case in selected:
            try:
                value = _truth_value(admin, case)
                ok = _same_as_pinned(case, value)
            except Exception as error:  # noqa: BLE001
                value, ok = None, False
                print(
                    f"[FAIL] #{case.id} truth query: {type(error).__name__}: {error}",
                    file=sys.stderr,
                )
            validation[case.id] = {
                "expected": value,
                "pinned_ok": ok,
                "pinned": case.expected,
            }
            if not ok:
                print(
                    f"[FAIL] #{case.id} pinned={case.expected!r} database={value!r}",
                    file=sys.stderr,
                )
        if not all(item["pinned_ok"] for item in validation.values()):
            print("\nTruth validation failed; not spending model calls.", file=sys.stderr)
            return 1
        print(f"[ok] {len(selected)} non-hold-out truth values match")

        base_prompt = get_system_prompt(engine=admin, now=PROMPT_TIME)
        verifier_prompt = (
            base_prompt
            + "\n\n"
            + MULTI_RULES
            + "\n\nDELIMITED ROLE ADDENDUM (procedure only; not new schema facts):\n"
            + ROLE_PROMPTS["verification"]
        )
        meta = _meta(
            base_prompt=base_prompt,
            verifier_prompt=verifier_prompt,
            selected=selected,
            repeats=args.repeats,
            solver_steps=args.solver_steps,
            verifier_steps=args.verifier_steps,
            validation=validation,
        )
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "prompt.txt").write_text(base_prompt, encoding="utf-8")
        (out_dir / "single_roles.txt").write_text(
            MERGED_ROLE_ADDENDUM, encoding="utf-8"
        )
        (out_dir / "verifier_prompt.txt").write_text(
            verifier_prompt, encoding="utf-8"
        )
        _write_json(out_dir / "run_meta.json", meta)
        print(
            f"model: {meta['model']} at {meta['base_url']}\n"
            f"budget: {args.solver_steps} solver + {args.verifier_steps} verifier\n"
            f"repeats: {args.repeats} | cases: {len(selected)}"
        )

        runs: list[dict] = []
        for repeat in range(1, args.repeats + 1):
            run_cases: list[dict] = []
            for case in selected:
                path = _case_path(out_dir, repeat, case.id)
                if path.exists():
                    finished = json.loads(path.read_text(encoding="utf-8"))
                    print(
                        f"[resume] repeat {repeat} case {case.id}: "
                        f"{'pass' if finished.get('pass') else 'fail'}"
                    )
                else:
                    print(f"\n[run] repeat {repeat} case {case.id}: {case.question}")
                    finished = await run_case(
                        case,
                        admin,
                        solver_steps=args.solver_steps,
                        verifier_steps=args.verifier_steps,
                        show_trace=args.show_trace,
                    )
                    finished["repeat"] = repeat
                    _write_json(path, finished)
                run_cases.append(finished)
                print(
                    f"  -> {'PASS' if finished.get('pass') else 'FAIL'} | "
                    f"steps {finished.get('steps')} | "
                    f"{finished.get('seconds', 0):.1f}s | "
                    f"{finished.get('reason', '')}"
                )
            runs.append(
                {
                    "repeat": repeat,
                    "passed": sum(item["pass"] for item in run_cases),
                    "total": len(run_cases),
                    "cases": run_cases,
                }
            )
            summary = _summary(runs, args.repeats, selected)
            _write_json(
                out_dir / "run.json",
                {**meta, "summary": summary, "runs": runs},
            )
            print(
                f"\nrepeat {repeat}: "
                f"{runs[-1]['passed']}/{runs[-1]['total']} passed"
            )

        summary = _summary(runs, args.repeats, selected)
        _write_json(
            out_dir / "run.json",
            {**meta, "summary": summary, "runs": runs},
        )
        _print_summary(summary)
        print(f"\nARTIFACT: {out_dir / 'run.json'}")
        return 0
    finally:
        admin.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / "single_verified" / "day5b_v3_sv_nonholdout_3x"),
        help="artifact directory",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--solver-steps", type=int, default=14)
    parser.add_argument("--verifier-steps", type=int, default=6)
    parser.add_argument(
        "--only",
        type=int,
        nargs="*",
        default=None,
        help="optional case ids; default is every non-hold-out case",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--show-trace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    if args.solver_steps < 1 or args.verifier_steps < 1:
        raise SystemExit("solver and verifier budgets must be >= 1")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
