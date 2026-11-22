"""Run the v5 benchmark for one arm and write a resumable artifact.

    uv run python evals/run_v5.py --tasks evals/v5/tasks_v5.json --split test --arm single --out runs/v5/qwen35b_single
    uv run python evals/run_v5.py --tasks evals/v5/tasks_v5.json --split test --arm multi  --out runs/v5/qwen35b_multi --verifier-mode safe
    uv run python evals/compare_v5.py --pair runs/v5/qwen35b_single runs/v5/qwen35b_multi

Differences from run_baseline.py (all deliberate, all in the paper's protocol):

* scoring reads the task's named output variables from the runtime and compares
  each with a tolerance (``evals.v5.tasks.validate``); prose is never scored;
* one attempt per task (more tasks beat more repeats for power);
* both arms use ``domains.hk.roles_v5`` and the same output contract text; the
  single arm always gets the merged-role addendum, so the only difference is
  topology (coordinator + workers + verifier on one shared runtime);
* runs with the v5 runtime (clean ``exit()``, numpy binds);
* provider-side failures (exception, zero tokens, zero steps) are re-run up to
  ``--infra-retries`` times and logged, never counted as agent failures, exactly as
  the paper excludes and re-runs infrastructure failures symmetrically;
* temperature 0.2 by default (paper); ``--extra-body`` lets you switch off a
  provider's thinking mode so a strong backbone is comparable with local runs.
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
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent import BUDGET_EXHAUSTED_SENTINEL, run_worker  # noqa: E402
from core.llm import get_client, snapshot_usage, USAGE  # noqa: E402
from core.orchestrator import run_pipeline  # noqa: E402
from core.runtime_v5 import PythonRuntimeV5  # noqa: E402
from domains.hk import roles_v5  # noqa: E402
from evals.v5.tasks import MISSING, Task, load_tasks, output_contract, validate  # noqa: E402

LLM = Callable[[list[dict[str, str]]], Awaitable[str]]


def make_llm(model: str, temperature: float, extra_body: dict | None) -> LLM:
    """Same accounting as core.llm.llm, plus provider options (e.g. disable thinking)."""
    import openai

    async def call(messages: list[dict[str, str]]) -> str:
        client = get_client()
        last: Exception | None = None
        for attempt in range(2):
            started = time.perf_counter()
            try:
                kwargs: dict[str, Any] = dict(model=model, messages=messages, temperature=temperature)
                if extra_body:
                    kwargs["extra_body"] = extra_body
                resp = await client.chat.completions.create(**kwargs)
                USAGE["prompt"] += resp.usage.prompt_tokens if resp.usage else 0
                USAGE["completion"] += resp.usage.completion_tokens if resp.usage else 0
                USAGE["calls"] += 1
                USAGE["total_seconds"] += time.perf_counter() - started
                return resp.choices[0].message.content or ""
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                last = exc
                await asyncio.sleep(1.0)
        raise RuntimeError(f"llm failed after retry: {last}")

    return call


def _vars_for_validation(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


async def run_task(
    task: Task,
    *,
    arm: str,
    llm: LLM,
    runtime_factory: Callable[[], PythonRuntimeV5],
    base_system: Callable[[PythonRuntimeV5], str],
    max_steps: int = 20,
    verify_reserve: int = 6,
    synth_reserve: int = 3,
    verifier_mode: str = "safe",
) -> dict[str, Any]:
    runtime = runtime_factory()
    trace: list[tuple[str, Any]] = []
    try:
        base = base_system(runtime)
        suffix = output_contract(task)
        usage0 = snapshot_usage()
        started = time.perf_counter()
        pipeline = None
        if arm == "multi":
            pipeline = await run_pipeline(
                llm=llm,
                runtime=runtime,
                role_prompts=roles_v5.ROLE_PROMPTS,
                shared_rules=base + "\n\n" + roles_v5.MULTI_RULES,
                coordinator_system=roles_v5.COORDINATOR_SYSTEM,
                query=task.question,
                task_suffix=suffix,
                max_total=max_steps,
                verify_reserve=verify_reserve,
                synth_reserve=synth_reserve,
                verifier_mode=verifier_mode,
                answer_vars=task.var_names(),
                emit=lambda kind, payload: trace.append((kind, payload)),
            )
            final_text, steps = pipeline.final_text, pipeline.solver_steps
            executions = sum(r.executions for r in pipeline.worker_runs)
            first = next((r.first_attempt_ok for r in pipeline.worker_runs if r.executions > 0), None)
        else:
            res = await run_worker(
                llm=llm,
                runtime=runtime,
                system=base + "\n\n" + roles_v5.MERGED_ROLE_ADDENDUM_V5,
                task=task.question + suffix,
                max_steps=max_steps,
                emit=lambda kind, payload: trace.append((kind, payload)),
            )
            final_text, steps, executions, first = res.final_text, res.steps, res.executions, res.first_attempt_ok
        elapsed = time.perf_counter() - started
        usage1 = snapshot_usage()
        tokens = (usage1["prompt"] - usage0["prompt"]) + (usage1["completion"] - usage0["completion"])
        calls = usage1["calls"] - usage0["calls"]

        result = validate(runtime.ns, task)
        out: dict[str, Any] = {
            "task_id": task.id, "family": task.family, "category": task.category,
            "difficulty": task.difficulty, "split": task.split, "arm": arm,
            "pass": result.success, "validator": result.as_dict(),
            "steps": steps, "executions": executions, "first_attempt_ok": first,
            "tokens": tokens, "calls": calls, "seconds": elapsed,
            "final_text": final_text,
            "budget_exhausted": final_text.strip() == BUDGET_EXHAUSTED_SENTINEL,
            "last_error": next((str(p)[:600] for k, p in reversed(trace) if k == "output" and "ERROR" in str(p)), ""),
        }
        if pipeline is not None:
            pre = {k: MISSING if v is None else v for k, v in pipeline.vars_pre_verify.items()}
            pre_result = validate({k: v for k, v in pre.items() if v is not MISSING}, task)
            out.update(
                pass_pre_verify=pre_result.success,
                verifier_mode=pipeline.verifier_mode,
                verifier_changed=pipeline.verifier_changed_answer,
                verifier_rejections=pipeline.verifier_rejections,
                stall_count=pipeline.stall_count,
                plan=pipeline.plan, plan_source=pipeline.plan_source,
                planner_tokens=pipeline.planner_tokens,
                worker_runs=[w.as_dict() for w in pipeline.worker_runs],
            )
        return out
    finally:
        runtime.close()


def is_infra_failure(out: dict[str, Any]) -> bool:
    """Provider-side failure: nothing executed and nothing billed (paper: re-run, not scored)."""
    return out.get("tokens", 0) == 0 and out.get("steps", 0) == 0


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def summarize(outs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(outs)
    if not n:
        return {"n": 0}

    def rate(pred) -> float:
        return round(sum(1 for o in outs if pred(o)) / n, 4)

    by_cat: dict[str, list[int]] = {}
    for o in outs:
        by_cat.setdefault(o["category"], []).append(int(o["pass"]))
    s: dict[str, Any] = {
        "n": n,
        "pass": sum(o["pass"] for o in outs),
        "accuracy": rate(lambda o: o["pass"]),
        "first_attempt_success": rate(lambda o: o.get("first_attempt_ok") is True),
        "mean_tokens": round(sum(o["tokens"] for o in outs) / n),
        "median_tokens": sorted(o["tokens"] for o in outs)[n // 2],
        "mean_seconds": round(sum(o["seconds"] for o in outs) / n, 1),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
        "infra_retries_total": sum(o.get("infra_retries", 0) for o in outs),
    }
    if outs[0]["arm"] == "multi":
        s["pass_pre_verify"] = sum(1 for o in outs if o.get("pass_pre_verify"))
        s["stalls"] = sum(o.get("stall_count", 0) for o in outs)
        s["verifier_rejections"] = sum(len(o.get("verifier_rejections", [])) for o in outs)
    return s


async def main_async(args: argparse.Namespace) -> int:
    from core.env import load_agent_env
    from core.prompts import HKT
    from domains.hk.schema_prompt import get_system_prompt
    from domains.hk.tools import build_runtime

    load_agent_env(override=False, scrub=True)
    if "DB_URL_ADMIN" in os.environ:
        print("[FAIL] DB_URL_ADMIN is exported; the agent environment is not scrubbed", file=sys.stderr)
        return 1
    model = args.model or os.environ.get("LLM_MODEL", "qwen2.5-coder:14b")
    if args.base_url:
        os.environ["LLM_BASE_URL"] = args.base_url
    if args.api_key_env:
        os.environ["LLM_API_KEY"] = os.environ[args.api_key_env]
    extra = json.loads(args.extra_body) if args.extra_body else None

    tasks = load_tasks(Path(args.tasks))
    if args.split != "all":
        tasks = [t for t in tasks if t.split == args.split]
    if args.only:
        tasks = [t for t in tasks if t.id in set(args.only)]
    if args.limit:
        tasks = tasks[: args.limit]
    out_dir = Path(args.out)
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)

    prompt_time = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=HKT)

    def runtime_factory() -> PythonRuntimeV5:
        rt = build_runtime()
        return PythonRuntimeV5(variables=dict(rt.ns))

    def base_system(rt: PythonRuntimeV5) -> str:
        return get_system_prompt(engine=rt.ns["engine"], now=prompt_time)

    probe = runtime_factory()
    try:
        base_sha = hashlib.sha256(base_system(probe).encode()).hexdigest()
    finally:
        probe.close()
    meta = {
        "model": model, "arm": args.arm, "temperature": args.temperature, "extra_body": extra,
        "max_steps": args.max_steps, "verifier_mode": args.verifier_mode, "split": args.split,
        "tasks_file": str(args.tasks), "n_tasks": len(tasks),
        "base_prompt_sha256": base_sha,
        "single_addendum_sha256": hashlib.sha256(roles_v5.MERGED_ROLE_ADDENDUM_V5.encode()).hexdigest(),
        "multi_prompts_sha256": hashlib.sha256(
            (roles_v5.COORDINATOR_SYSTEM + roles_v5.MULTI_RULES + "".join(roles_v5.ROLE_PROMPTS.values())).encode()
        ).hexdigest(),
    }
    meta_path = out_dir / "run_meta.json"
    if meta_path.exists():
        old = json.loads(meta_path.read_text())
        keys = ("model", "arm", "temperature", "max_steps", "verifier_mode", "base_prompt_sha256")
        if any(old.get(k) != meta.get(k) for k in keys):
            print("[FAIL] resume refused: run_meta.json differs from current settings", file=sys.stderr)
            return 1
    _atomic_write(meta_path, meta)

    llm = make_llm(model, args.temperature, extra)
    outs: list[dict[str, Any]] = []
    for i, task in enumerate(tasks, 1):
        path = out_dir / "cases" / f"task-{task.id:04d}.json"
        if path.exists():
            outs.append(json.loads(path.read_text()))
            continue
        retries = 0
        while True:
            try:
                out = await run_task(
                    task, arm=args.arm, llm=llm, runtime_factory=runtime_factory, base_system=base_system,
                    max_steps=args.max_steps, verifier_mode=args.verifier_mode,
                )
            except Exception as exc:  # noqa: BLE001 - provider/transport failure
                out = {"task_id": task.id, "category": task.category, "arm": args.arm, "pass": False,
                       "tokens": 0, "steps": 0, "seconds": 0.0, "error": f"{type(exc).__name__}: {exc}"}
            if is_infra_failure(out) and retries < args.infra_retries:
                retries += 1
                await asyncio.sleep(2.0)
                continue
            break
        out["infra_retries"] = retries
        out["infra_failed"] = is_infra_failure(out)
        _atomic_write(path, out)
        outs.append(out)
        print(f"[{i}/{len(tasks)}] task {task.id:>3} {task.category:<11} {'PASS' if out['pass'] else 'fail'} "
              f"tokens={out['tokens']} steps={out.get('steps')} infra_retries={retries}", flush=True)

    scored = [o for o in outs if not o.get("infra_failed")]
    summary = {"meta": meta, "summary": summarize(scored), "infra_failed_task_ids": [o["task_id"] for o in outs if o.get("infra_failed")]}
    _atomic_write(out_dir / "run.json", {**summary, "cases": outs})
    print(json.dumps(summary["summary"], indent=1))
    if summary["infra_failed_task_ids"]:
        print("INFRA-FAILED (excluded; re-run with --resume):", summary["infra_failed_task_ids"])
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", default="evals/v5/tasks_v5.json")
    ap.add_argument("--split", choices=["dev", "test", "all"], default="test")
    ap.add_argument("--arm", choices=["single", "multi"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--extra-body", default=None, help='JSON passed as extra_body, e.g. \'{"thinking": {"type": "disabled"}}\' (check provider docs)')
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--verifier-mode", choices=["safe", "overwrite", "flag", "none"], default="safe")
    ap.add_argument("--only", type=int, nargs="*")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--infra-retries", type=int, default=2)
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
