"""Single agent loop and worker execution for Neuro-Air Lite.

A worker receives a task and a system prompt, writes exactly one Python block per turn,
which runs against the shared PythonRuntime. The stdout is returned as an observation,
and the loop continues until the agent produces a final textual answer or the step budget is exhausted.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.llm import snapshot_usage
from core.runtime import PythonRuntime

BUDGET_EXHAUSTED_SENTINEL = "[STEP_BUDGET_EXHAUSTED]"


@dataclass
class WorkerResult:
    final_text: str
    steps: int
    ok: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_seconds: float = 0.0
    history: list[dict[str, str]] = field(default_factory=list)
    # True when the first *executed* code block succeeded. Keyed on executions,
    # not on step number, so the no-code retry nudge does not mark a correct
    # first program as a failure.
    first_attempt_ok: bool | None = None
    executions: int = 0


def extract_code(text: str) -> str | None:
    """Return the body of the first executable (python or unlabelled) fenced block.

    Models routinely show the SQL they are about to run in a ```sql block next to
    the ```python block. Treating any fence as code executed `sql\\nSELECT ...` as
    Python, which fails with a SyntaxError and burns a step. Only `python`, `py`
    and unlabelled fences are executable.
    """
    lang: str | None = None
    buf: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if lang is None:
            if stripped.startswith("```"):
                lang = stripped[3:].strip().lower()
                buf = []
        elif stripped.startswith("```"):
            if lang in ("", "python", "py"):
                return "\n".join(buf).strip()
            lang = None
        else:
            buf.append(line)

    return None


async def run_worker(
    llm: Callable[[list[dict[str, str]]], Any],
    runtime: PythonRuntime,
    system: str,
    task: str,
    max_steps: int = 20,
    emit: Callable[[str, Any], None] = lambda *a: None,
) -> WorkerResult:
    """Run an agent worker loop with the persistent runtime.

    Returns WorkerResult with execution stats, final output, and token counts.
    Token counts come from the `core.llm` USAGE delta around the loop, so a
    scripted fake LLM (which never touches USAGE) correctly reports zero.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]

    usage_before = snapshot_usage()
    steps = 0
    executions = 0
    first_attempt_ok: bool | None = None
    total_started = time.perf_counter()
    nudged_no_code = False

    def finish(final_text: str, ok: bool) -> WorkerResult:
        usage_after = snapshot_usage()
        return WorkerResult(
            final_text=final_text,
            steps=steps,
            ok=ok,
            prompt_tokens=usage_after["prompt"] - usage_before["prompt"],
            completion_tokens=usage_after["completion"] - usage_before["completion"],
            total_seconds=time.perf_counter() - total_started,
            history=messages,
            first_attempt_ok=first_attempt_ok,
            executions=executions,
        )

    while steps < max_steps:
        text = await llm(messages)
        steps += 1

        code = extract_code(text)

        # If step 1 returns no code block, give the model one explicit nudge to write code
        if not code:
            if steps == 1 and not nudged_no_code:
                nudged_no_code = True
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Please write executable Python code inside a ```python ... ``` code block "
                            "to query the database and compute the answer. Do not guess or answer without running code."
                        ),
                    }
                )
                continue
            # Subsequent turn without code block is considered the final answer
            return finish(text.strip(), True)

        emit("code", code)
        is_first_execution = executions == 0
        res = await asyncio.to_thread(runtime.execute, code, first_attempt=is_first_execution)
        if is_first_execution:
            first_attempt_ok = res.ok
        executions += 1
        emit("output", res.render(2000))

        # Preserve history: record assistant code block and user observation
        messages.append({"role": "assistant", "content": f"```python\n{code}\n```"})
        messages.append({"role": "user", "content": f"OUTPUT:\n{res.render()}"})

    return finish(BUDGET_EXHAUSTED_SENTINEL, False)
