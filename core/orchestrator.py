"""Hierarchical multi-agent orchestration for Neuro-Air Lite.

The coordinator produces a small JSON plan. Workers run sequentially against one
shared ``PythonRuntime``; the verifier gets the remaining budget and audits the
staged result with a fresh database query. The 20-step solver budget follows the
Day 5 plan:

    producer pool 11 + synthesis reserve 3 + verifier reserve 6 = 20

The coordinator's own planning call is orchestration overhead. It is recorded in
``planner_*`` fields and in the token/call totals, but it is not charged against
the solver-step budget. This keeps the PDF's producer pool intact while making
the extra call visible for a later all-call sensitivity check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from core.agent import BUDGET_EXHAUSTED_SENTINEL, WorkerResult, extract_code, run_worker
from core.llm import snapshot_usage
from core.runtime import PythonRuntime

PLAN_ROLES = (
    "data_acquisition",
    "knowledge_retrieval",
    "pollution_analysis",
    "multivariate_meteo",
    "synthesis_report",
)
FALLBACK_ROLES = (
    "data_acquisition",
    "pollution_analysis",
    "multivariate_meteo",
    "synthesis_report",
)
MAX_PLAN_STEPS = 6
DEFAULT_MAX_TOTAL = 20
DEFAULT_VERIFY_RESERVE = 6
DEFAULT_SYNTH_RESERVE = 3

LLM = Callable[[list[dict[str, str]]], Awaitable[str]]
Emit = Callable[[str, Any], None]


@dataclass
class WorkerRun:
    """One worker's result, without the full chat history."""

    role: str
    task: str
    budget: int
    steps: int
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    final_text: str
    ok: bool
    first_attempt_ok: bool | None
    executions: int

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "task": self.task,
            "budget": self.budget,
            "steps": self.steps,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens": self.tokens,
            "seconds": self.seconds,
            "final_text": self.final_text,
            "ok": self.ok,
            "first_attempt_ok": self.first_attempt_ok,
            "executions": self.executions,
        }


@dataclass
class PipelineResult:
    final_text: str
    plan: list[dict[str, str]]
    plan_source: str
    worker_runs: list[WorkerRun]
    solver_steps: int
    planner_calls: int
    planner_prompt_tokens: int
    planner_completion_tokens: int
    total_calls: int
    total_tokens: int
    ok: bool
    verifier_text: str = ""
    plan_error: str = ""

    @property
    def planner_tokens(self) -> int:
        return self.planner_prompt_tokens + self.planner_completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "final_text": self.final_text,
            "plan": self.plan,
            "plan_source": self.plan_source,
            "plan_error": self.plan_error,
            "worker_runs": [run.as_dict() for run in self.worker_runs],
            "solver_steps": self.solver_steps,
            "planner_calls": self.planner_calls,
            "planner_prompt_tokens": self.planner_prompt_tokens,
            "planner_completion_tokens": self.planner_completion_tokens,
            "planner_tokens": self.planner_tokens,
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
            "verifier_text": self.verifier_text,
            "ok": self.ok,
        }


def fallback_plan(query: str) -> list[dict[str, str]]:
    """A conservative plan used when the coordinator returns unusable JSON."""
    return [{"role": role, "task": query} for role in FALLBACK_ROLES]


def parse_plan(text: str, query: str, max_steps: int = MAX_PLAN_STEPS) -> list[dict[str, str]]:
    """Parse and validate a coordinator response.

    The coordinator is asked for a bare JSON array. Models sometimes wrap it in
    prose or a code fence, so take the first ``[`` through the last ``]``. Invalid
    roles or empty tasks are skipped. Exactly one synthesis_report step is forced
    to the end and the result is capped at ``max_steps``.
    """
    if not isinstance(text, str):
        raise ValueError("coordinator response is not text")
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("coordinator response has no JSON array")
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"coordinator JSON is invalid: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("coordinator JSON is not an array")

    steps: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        task = item.get("task")
        if role not in PLAN_ROLES or role == "synthesis_report":
            continue
        if not isinstance(task, str) or not task.strip():
            continue
        steps.append({"role": role, "task": task.strip()})
        if len(steps) >= max_steps - 1:
            break

    if not steps:
        raise ValueError("coordinator plan has no valid worker steps")

    # The planner may have supplied a synthesis task; preserve one if present.
    synthesis_task = query
    for item in raw:
        if isinstance(item, dict) and item.get("role") == "synthesis_report":
            task = item.get("task")
            if isinstance(task, str) and task.strip():
                synthesis_task = task.strip()
            break

    steps = steps[: max_steps - 1]
    steps.append({"role": "synthesis_report", "task": synthesis_task})
    return steps


async def make_plan(
    llm: LLM,
    coordinator_system: str,
    query: str,
    max_steps: int = MAX_PLAN_STEPS,
) -> list[dict[str, str]]:
    """Ask the coordinator for a plan; callers handle parse failures."""
    text = await llm(
        [
            {"role": "system", "content": coordinator_system},
            {"role": "user", "content": query},
        ]
    )
    return parse_plan(text, query, max_steps=max_steps)


def _manifest_text(runtime: PythonRuntime) -> str:
    manifest = runtime.manifest()
    if not manifest:
        return "(none)"
    return ", ".join(f"{name}: {kind}" for name, kind in manifest.items())


def _worker_system(
    role_prompt: str,
    shared_rules: str,
    runtime: PythonRuntime,
    task: str,
    *,
    verifier: bool = False,
) -> str:
    stage = (
        "STAGED VALUES (do NOT trust; recompute from the raw database):"
        if verifier
        else "VARIABLES ALREADY IN THE SHARED RUNTIME:"
    )
    return "\n\n".join(
        [
            role_prompt,
            shared_rules,
            f"{stage}\n{_manifest_text(runtime)}",
            f"YOUR SUBTASK:\n{task}",
        ]
    )


def _stall_guarded_llm(llm: LLM, role: str, emit: Emit) -> LLM:
    """Stop a worker that repeats the identical code block.

    The model occasionally retries a failing query verbatim until its budget is
    gone. The shared runtime and the verifier can still recover, but that wastes
    the matched budget for the whole hierarchy. The guard returns a final text
    (not a code block) on the second identical block, so the worker exits and the
    next role gets its reserved steps. This is a runtime safeguard, not a domain
    hint: it does not tell the worker what SQL or answer to use.
    """
    last_code: str | None = None
    repeats = 0

    async def guarded(messages: list[dict[str, str]]) -> str:
        nonlocal last_code, repeats
        text = await llm(messages)
        code = extract_code(text)
        if code is not None:
            if code == last_code:
                repeats += 1
            else:
                last_code = code
                repeats = 0
            if repeats >= 1:
                emit("worker_stall", {"role": role, "repeats": repeats})
                return (
                    "I stopped because the identical code block was about to run again. "
                    "Use the values already staged, change the query, or report the limitation."
                )
        return text

    return guarded


def _run_record(role: str, task: str, budget: int, result: WorkerResult) -> WorkerRun:
    return WorkerRun(
        role=role,
        task=task,
        budget=budget,
        steps=result.steps,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        seconds=result.total_seconds,
        final_text=result.final_text,
        ok=result.ok,
        first_attempt_ok=result.first_attempt_ok,
        executions=result.executions,
    )


async def run_pipeline(
    llm: LLM,
    runtime: PythonRuntime,
    role_prompts: Mapping[str, str],
    shared_rules: str,
    coordinator_system: str,
    query: str,
    *,
    task_suffix: str = "",
    max_total: int = DEFAULT_MAX_TOTAL,
    verify_reserve: int = DEFAULT_VERIFY_RESERVE,
    synth_reserve: int = DEFAULT_SYNTH_RESERVE,
    emit: Emit = lambda *a: None,
) -> PipelineResult:
    """Run planner -> workers -> independent verifier on one shared runtime.

    ``max_total`` is the solver-step budget (workers plus verifier). The planner
    call is recorded separately. The worker loop's step count is authoritative,
    so a no-code nudge or a malformed turn is charged like any other LLM call.
    """
    max_total = max(1, int(max_total))
    verify_reserve = max(0, min(int(verify_reserve), max_total - 1))
    plan_budget = max(1, max_total - verify_reserve)
    synth_reserve = max(0, min(int(synth_reserve), plan_budget - 1))
    producer_pool = max(0, plan_budget - synth_reserve)

    usage_before = snapshot_usage()
    plan_error = ""
    try:
        plan = await make_plan(llm, coordinator_system, query, max_steps=MAX_PLAN_STEPS)
        plan_source = "llm"
    except Exception as exc:  # planner failure must not kill the whole run
        plan = fallback_plan(query)
        plan_source = "fallback"
        plan_error = f"{type(exc).__name__}: {exc}"
    usage_after_plan = snapshot_usage()

    planner_calls = 1
    planner_prompt_tokens = usage_after_plan["prompt"] - usage_before["prompt"]
    planner_completion_tokens = usage_after_plan["completion"] - usage_before["completion"]

    emit("plan", plan)

    used = 0
    worker_runs: list[WorkerRun] = []
    final_text = ""

    for index, step in enumerate(plan):
        if used >= plan_budget:
            break
        remaining_plan = plan_budget - used
        is_last = index == len(plan) - 1
        if is_last:
            # The synthesis step may use the whole remaining plan budget,
            # including the synthesis reserve.
            budget = remaining_plan
        else:
            budget = min(remaining_plan, producer_pool - used)
        if budget < 1:
            continue

        role = step["role"]
        task = step["task"]
        role_prompt = role_prompts.get(role)
        if not role_prompt:
            continue
        if role == "synthesis_report" and task_suffix:
            task = task + task_suffix
        system = _worker_system(role_prompt, shared_rules, runtime, task)
        emit("worker_start", {"role": role, "task": task, "budget": budget})
        result = await run_worker(
            llm=_stall_guarded_llm(llm, role, emit),
            runtime=runtime,
            system=system,
            task=task,
            max_steps=budget,
            emit=emit,
        )
        used += result.steps
        worker_runs.append(_run_record(role, task, budget, result))
        if role == "synthesis_report" and result.final_text.strip():
            final_text = result.final_text.strip()
        if used >= plan_budget:
            break

    # The verifier is independent and gets only the reserve that remains. The
    # reserve is capped so a verifier can never turn a 20-step run into 21.
    verify_budget = min(verify_reserve, max_total - used)
    verifier_text = ""
    if verify_budget > 0:
        verify_task = query + task_suffix
        verify_system = _worker_system(
            role_prompts["verification"],
            shared_rules,
            runtime,
            verify_task,
            verifier=True,
        )
        emit(
            "worker_start",
            {"role": "verification", "task": verify_task, "budget": verify_budget},
        )
        verifier = await run_worker(
            llm=_stall_guarded_llm(llm, "verification", emit),
            runtime=runtime,
            system=verify_system,
            task=verify_task,
            max_steps=verify_budget,
            emit=emit,
        )
        used += verifier.steps
        worker_runs.append(_run_record("verification", verify_task, verify_budget, verifier))
        verifier_text = verifier.final_text.strip()
        if verifier_text:
            final_text = verifier_text

    ok = bool(final_text.strip()) and final_text.strip() != BUDGET_EXHAUSTED_SENTINEL
    if not ok and not final_text.strip():
        final_text = BUDGET_EXHAUSTED_SENTINEL
        ok = False

    emit("final", final_text)

    worker_tokens = sum(run.tokens for run in worker_runs)
    return PipelineResult(
        final_text=final_text,
        plan=plan,
        plan_source=plan_source,
        worker_runs=worker_runs,
        solver_steps=used,
        planner_calls=planner_calls,
        planner_prompt_tokens=planner_prompt_tokens,
        planner_completion_tokens=planner_completion_tokens,
        total_calls=planner_calls + used,
        total_tokens=planner_prompt_tokens + planner_completion_tokens + worker_tokens,
        ok=ok,
        verifier_text=verifier_text,
        plan_error=plan_error,
    )
