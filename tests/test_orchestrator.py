"""Tests for the Day 5 hierarchical orchestrator using a scripted fake LLM."""

from __future__ import annotations

import pytest

from core.orchestrator import fallback_plan, parse_plan, run_pipeline
from core.runtime import PythonRuntime


class ScriptedLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.messages.append(messages)
        if not self.responses:
            raise AssertionError("scripted LLM ran out of responses")
        return self.responses.pop(0)


def _system() -> str:
    return "TEST DOMAIN PROMPT"


def _roles() -> dict[str, str]:
    return {
        "data_acquisition": "stage data",
        "knowledge_retrieval": "retrieve knowledge",
        "pollution_analysis": "compute stats",
        "multivariate_meteo": "join weather",
        "synthesis_report": "produce final answer",
        "verification": "verify independently",
    }


def test_parse_plan_caps_and_forces_synthesis_last():
    raw = """Here is the plan:
    [
      {"role": "data_acquisition", "task": "load a"},
      {"role": "knowledge_retrieval", "task": "load b"},
      {"role": "pollution_analysis", "task": "compute c"},
      {"role": "multivariate_meteo", "task": "join d"},
      {"role": "synthesis_report", "task": "answer"},
      {"role": "pollution_analysis", "task": "extra"},
      {"role": "multivariate_meteo", "task": "extra2"}
    ]
    """
    plan = parse_plan(raw, "question", max_steps=6)
    assert len(plan) == 6
    assert plan[-1]["role"] == "synthesis_report"
    assert all(step["role"] != "verification" for step in plan)


def test_parse_plan_rejects_bad_json_and_empty_plan():
    with pytest.raises(ValueError):
        parse_plan("not json", "question")
    with pytest.raises(ValueError):
        parse_plan("[{\"role\": \"not_a_role\", \"task\": \"x\"}]", "question")


def test_fallback_plan_is_bounded_and_synthesis_last():
    plan = fallback_plan("question")
    assert plan[-1]["role"] == "synthesis_report"
    assert len(plan) <= 6
    assert "knowledge_retrieval" not in [step["role"] for step in plan]


async def test_pipeline_runs_workers_on_shared_runtime_and_emits_events():
    llm = ScriptedLLM(
        [
            '[{"role": "data_acquisition", "task": "stage 41"}, '
            '{"role": "synthesis_report", "task": "answer"}]',
            "```python\nstaged = 41\nprint(staged)\n```",
            "staged value ready",
            "```python\nANSWER = staged + 1\nprint(ANSWER)\n```",
            "The answer is 42.",
        ]
    )
    runtime = PythonRuntime()
    events: list[tuple[str, object]] = []

    result = await run_pipeline(
        llm=llm,
        runtime=runtime,
        role_prompts=_roles(),
        shared_rules=_system(),
        coordinator_system="coordinator",
        query="test question",
        max_total=10,
        verify_reserve=0,
        synth_reserve=0,
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    assert runtime.ns["staged"] == 41
    assert runtime.ns["ANSWER"] == 42
    assert result.final_text == "The answer is 42."
    assert result.ok is True
    assert result.solver_steps == 4
    assert result.plan_source == "llm"
    assert [run.role for run in result.worker_runs] == [
        "data_acquisition",
        "synthesis_report",
    ]
    assert [kind for kind, _ in events] == [
        "plan",
        "worker_start",
        "code",
        "output",
        "worker_start",
        "code",
        "output",
        "final",
    ]


async def test_verifier_gets_remaining_budget_and_task_suffix():
    llm = ScriptedLLM(
        [
            '[{"role": "data_acquisition", "task": "stage 7"}, '
            '{"role": "synthesis_report", "task": "answer"}]',
            "```python\nstaged = 7\nprint(staged)\n```",
            "staged seven",
            "```python\nANSWER = 7\nprint(ANSWER)\n```",
            "```python\nprint('VERIFY ANSWER: stored=7 | independent=7 | final=7')\n```",
            "verified seven",
        ]
    )
    runtime = PythonRuntime()

    result = await run_pipeline(
        llm=llm,
        runtime=runtime,
        role_prompts=_roles(),
        shared_rules=_system(),
        coordinator_system="coordinator",
        query="test question",
        task_suffix="\nANSWER HINT",
        max_total=6,
        verify_reserve=3,
        synth_reserve=1,
        emit=lambda *a: None,
    )

    assert runtime.ns["ANSWER"] == 7
    assert result.final_text == "verified seven"
    assert [run.role for run in result.worker_runs] == [
        "data_acquisition",
        "synthesis_report",
        "verification",
    ]
    assert result.worker_runs[-1].task.endswith("ANSWER HINT")
    assert result.solver_steps <= 6
    assert result.total_calls == 1 + result.solver_steps


async def test_planner_failure_uses_fallback_plan():
    responses = ["this is not JSON"]
    # Four fallback workers, each needs one code turn and one final turn.
    for _ in range(4):
        responses.extend(["```python\nprint('staged')\n```", "done"])
    llm = ScriptedLLM(responses)
    runtime = PythonRuntime()

    result = await run_pipeline(
        llm=llm,
        runtime=runtime,
        role_prompts=_roles(),
        shared_rules=_system(),
        coordinator_system="coordinator",
        query="test question",
        max_total=20,
        verify_reserve=0,
        synth_reserve=0,
        emit=lambda *a: None,
    )

    assert result.plan_source == "fallback"
    assert result.plan_error
    assert [step["role"] for step in result.plan] == [
        "data_acquisition",
        "pollution_analysis",
        "multivariate_meteo",
        "synthesis_report",
    ]
    assert result.solver_steps <= 20


async def test_budget_is_never_exceeded_and_repeated_code_is_stopped():
    llm = ScriptedLLM(
        [
            '[{"role": "data_acquisition", "task": "loop"}, '
            '{"role": "synthesis_report", "task": "answer"}]'
        ]
        + ["```python\nprint('x')\n```"] * 50
    )
    runtime = PythonRuntime()

    result = await run_pipeline(
        llm=llm,
        runtime=runtime,
        role_prompts=_roles(),
        shared_rules=_system(),
        coordinator_system="coordinator",
        query="test question",
        max_total=5,
        verify_reserve=2,
        synth_reserve=1,
        emit=lambda *a: None,
    )

    assert result.solver_steps <= 5
    assert result.total_calls == 1 + result.solver_steps
    assert result.final_text
    assert "identical code block" in result.final_text
    assert any(
        run.role == "data_acquisition" and run.steps <= 2
        for run in result.worker_runs
    )
