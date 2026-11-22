"""End-to-end v5 plumbing with a scripted fake LLM (no DB, no network).

Proves: both arms run through run_task with the variable validator, exit() no longer
poisons a worker, and verifier_mode="safe" keeps a correct staged value that
verifier_mode="overwrite" lets the verifier destroy.
"""

import json
from pathlib import Path

import pytest

from core.runtime_v5 import PythonRuntimeV5
from evals import compare_v5, run_v5
from evals.v5 import tasks as T

TASK = T.Task(
    id=7, family="f", category="statistics", difficulty="medium", question="Q?",
    outputs=(
        T.Output("mean_x", "mean x", "float", "s", expected=12.5, tol_abs=0.05),
        T.Output("n_days", "days", "int", "s", expected=31),
    ),
)


def make_fake(*, synth_code: str, verifier_code: str | None):
    state: dict[str, int] = {}

    async def fake(messages):
        system = messages[0]["content"]
        if "central coordinator" in system:
            return json.dumps([{"role": "data_acquisition", "task": "load"}, {"role": "synthesis_report", "task": "answer"}])
        if "<merged_role_procedures>" in system:  # single arm: all roles merged into one prompt
            role, code = "single", synth_code
        elif "independent verifier" in system:
            role, code = "verification", verifier_code
        elif "You own the final answer" in system:
            role, code = "synthesis", synth_code
        else:
            role, code = "acquire", "staged = 1\nprint(staged)"
        state[role] = state.get(role, 0) + 1
        if state[role] == 1 and code:
            return f"```python\n{code}\n```"
        return "Done."

    return fake


def runtime():
    return PythonRuntimeV5()


def base(_rt):
    return "BASE PROMPT"


async def go(arm, fake, mode="safe"):
    return await run_v5.run_task(TASK, arm=arm, llm=fake, runtime_factory=runtime, base_system=base, verifier_mode=mode)


GOOD = "mean_x = 12.5\nn_days = 31\nprint(mean_x, n_days)\nexit()\nmean_x = -1"


async def test_single_arm_passes_and_exit_does_not_break_it():
    out = await go("single", make_fake(synth_code=GOOD, verifier_code=None))
    assert out["pass"] and out["validator"]["success"]
    assert out["first_attempt_ok"] is True


async def test_single_arm_fails_on_wrong_value():
    out = await go("single", make_fake(synth_code="mean_x = 99.0\nn_days = 31", verifier_code=None))
    assert not out["pass"]
    assert out["validator"]["checks"][0]["reason"].startswith("|99.0-12.5|")


async def test_multi_arm_end_to_end_records_pre_and_post_verify():
    out = await go("multi", make_fake(synth_code=GOOD, verifier_code="print('VERIFY mean_x ok')"))
    assert out["pass"] and out["pass_pre_verify"]
    assert out["plan"][-1]["role"] == "synthesis_report"
    assert out["verifier_rejections"] == []


async def test_safe_mode_blocks_verifier_nan_but_overwrite_does_not():
    fake = lambda: make_fake(synth_code=GOOD, verifier_code="mean_x = float('nan')")
    safe = await go("multi", fake(), mode="safe")
    ow = await go("multi", fake(), mode="overwrite")
    assert safe["pass"] and safe["verifier_rejections"] == ["mean_x: verifier wrote NaN/inf"]
    assert not ow["pass"] and ow["pass_pre_verify"]  # overwrite turned a pass into a fail


async def test_safe_mode_blocks_string_over_number():
    out = await go("multi", make_fake(synth_code=GOOD, verifier_code="n_days = 'No data available'"), mode="safe")
    assert out["pass"] and any("text over a numeric" in r for r in out["verifier_rejections"])


async def test_flag_mode_always_restores_synthesis_values():
    out = await go("multi", make_fake(synth_code=GOOD, verifier_code="mean_x = 3.0"), mode="flag")
    assert out["pass"]


async def test_safe_mode_accepts_valid_verifier_repair():
    bad = "mean_x = 7.0\nn_days = 31"
    out = await go("multi", make_fake(synth_code=bad, verifier_code="mean_x = 12.5"), mode="safe")
    assert not out["pass_pre_verify"] and out["pass"]  # the verifier's real job still works


def test_infra_failure_rule():
    assert run_v5.is_infra_failure({"tokens": 0, "steps": 0})
    assert not run_v5.is_infra_failure({"tokens": 0, "steps": 3})  # scripted fake LLM bills nothing


def test_compare_pooled_stats(tmp_path: Path):
    def write(name, passes, tokens):
        d = tmp_path / name
        d.mkdir()
        cases = [{"task_id": i + 1, "category": "c", "pass": p, "tokens": tokens, "first_attempt_ok": True}
                 for i, p in enumerate(passes)]
        (d / "run.json").write_text(json.dumps({"cases": cases}))
        return d

    s = write("s", [1, 0, 0, 1, 0, 0, 0, 0, 1, 1], 100)
    m = write("m", [1, 1, 1, 1, 1, 0, 0, 0, 1, 0], 350)
    rows = compare_v5.paired(compare_v5.load_run(s), compare_v5.load_run(m))
    st = compare_v5.stats(rows)
    assert (st["wins"], st["losses"], st["gain_pp"]) == (3, 1, 20.0)
    assert 0.6 < st["mcnemar_p"] <= 0.63  # exact two-sided p for 3 vs 1 is 0.625
    lo, hi = compare_v5.cluster_bootstrap([rows], b=500)
    assert lo < 20.0 < hi
