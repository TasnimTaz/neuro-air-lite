"""Tests for agent loop and worker execution using scripted fake LLM."""

import pytest

from core import llm as llm_module
from core.agent import BUDGET_EXHAUSTED_SENTINEL, extract_code, run_worker
from core.runtime import PythonRuntime


def test_extract_code_variants():
    # Python fenced block
    c1 = "Some text before\n```python\nprint('hello')\n```\nSome text after"
    assert extract_code(c1) == "print('hello')"

    # Bare fenced block
    c2 = "```\nx = 10\nprint(x)\n```"
    assert extract_code(c2) == "x = 10\nprint(x)"

    # No code block
    c3 = "The final answer is 42."
    assert extract_code(c3) is None


def test_extract_code_ignores_non_python_fences():
    # A ```sql block shown with the final answer must not be executed as Python.
    assert extract_code("```sql\nSELECT 1\n```") is None
    assert extract_code("```json\n{}\n```") is None
    assert extract_code("```text\nmean = 12.3\n```") is None

    # ... and a python block later in the same message still wins.
    mixed = "```sql\nSELECT 1\n```\nthen:\n```python\nx = 1\nprint(x)\n```"
    assert extract_code(mixed) == "x = 1\nprint(x)"


@pytest.mark.asyncio
async def test_agent_single_turn_and_answer():
    responses = [
        "```python\nans = 2 + 2\nprint(f'Answer is {ans}')\n```",
        "The computed answer is 4.",
    ]

    async def fake_llm(messages):
        return responses.pop(0)

    rt = PythonRuntime()
    result = await run_worker(
        llm=fake_llm,
        runtime=rt,
        system="System",
        task="Calculate 2 + 2",
        max_steps=5,
    )

    assert result.ok
    assert result.steps == 2
    assert "The computed answer is 4." in result.final_text
    assert rt.ns["ans"] == 4
    assert result.executions == 1
    assert result.first_attempt_ok is True


@pytest.mark.asyncio
async def test_agent_first_turn_no_code_nudge():
    responses = [
        "I think the answer is probably 4, but I haven't computed it.",
        "```python\nans = 4 * 10\nprint(ans)\n```",
        "The final computed answer is 40.",
    ]

    async def fake_llm(messages):
        return responses.pop(0)

    rt = PythonRuntime()
    result = await run_worker(
        llm=fake_llm,
        runtime=rt,
        system="System",
        task="Calculate 4 * 10",
        max_steps=5,
    )

    assert result.ok
    assert result.steps == 3
    assert "The final computed answer is 40." in result.final_text
    assert rt.ns["ans"] == 40


@pytest.mark.asyncio
async def test_first_attempt_after_nudge_is_still_first_attempt():
    """The retry nudge is not a failed execution: the first code block was correct."""
    responses = [
        "Let me think about it first.",
        "```python\nx = 40 + 2\nprint(x)\n```",
        "The answer is 42.",
    ]

    async def fake_llm(messages):
        return responses.pop(0)

    result = await run_worker(
        llm=fake_llm,
        runtime=PythonRuntime(),
        system="System",
        task="Compute 40 + 2",
        max_steps=5,
    )

    assert result.steps == 3
    assert result.executions == 1
    assert result.first_attempt_ok is True


@pytest.mark.asyncio
async def test_first_attempt_is_false_when_first_program_fails():
    responses = [
        "```python\n1 / 0\n```",
        "```python\nprint(2)\n```",
        "Recovered.",
    ]

    async def fake_llm(messages):
        return responses.pop(0)

    result = await run_worker(
        llm=fake_llm,
        runtime=PythonRuntime(),
        system="System",
        task="Divide",
        max_steps=5,
    )

    assert result.executions == 2
    assert result.first_attempt_ok is False


@pytest.mark.asyncio
async def test_worker_result_reports_usage_delta():
    responses = [
        "```python\nprint('hi')\n```",
        "Done.",
    ]

    async def fake_llm(messages):
        llm_module.USAGE["prompt"] += 100
        llm_module.USAGE["completion"] += 10
        llm_module.USAGE["calls"] += 1
        return responses.pop(0)

    before = llm_module.snapshot_usage()
    result = await run_worker(
        llm=fake_llm,
        runtime=PythonRuntime(),
        system="System",
        task="Say hi",
        max_steps=5,
    )
    after = llm_module.snapshot_usage()

    assert result.prompt_tokens == after["prompt"] - before["prompt"] == 200
    assert result.completion_tokens == after["completion"] - before["completion"] == 20


@pytest.mark.asyncio
async def test_agent_step_budget_exhaustion():
    async def infinite_loop_llm(messages):
        return "```python\nx = 1\nprint(x)\n```"

    rt = PythonRuntime()
    result = await run_worker(
        llm=infinite_loop_llm,
        runtime=rt,
        system="System",
        task="Loop forever",
        max_steps=3,
    )

    assert not result.ok
    assert result.steps == 3
    assert result.final_text == BUDGET_EXHAUSTED_SENTINEL
    assert result.executions == 3
