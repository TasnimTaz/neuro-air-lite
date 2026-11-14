"""Compare two saved baseline arms without spending any model calls.

Usage:
    uv run python evals/compare_arms.py \
        --single runs/baseline/day4_v2_budget20 \
        --multi runs/multi/day5_v2_budget20_3x \
        --out runs/multi/day5_v2_budget20_3x/compare.json

The comparison re-scores every saved case with scorer v1, v2 and semantic. The
primary table uses ``--scorer`` (v2 by default), so a historical v1 artifact is
rescored rather than trusted as-is.

Repeats are not independent samples. The task-level majority test (exact
binomial/McNemar over tasks) and the paired task bootstrap are the primary
inference; the case-run percentages are descriptive.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent import BUDGET_EXHAUSTED_SENTINEL  # noqa: E402
from evals.hk_baseline_v2 import CASES, Case  # noqa: E402
from evals.run_baseline import SCORER_VERSIONS, _score  # noqa: E402

CASE_BY_ID = {case.id: case for case in CASES}


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    if value.is_dir():
        value = value / "run.json"
    return value


def _load(path: str | Path) -> dict:
    artifact = _resolve(path)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "runs" not in payload:
        raise ValueError(f"not a baseline artifact: {artifact}")
    return payload


def _case_runs(artifact: dict) -> list[dict]:
    return [
        item
        for run in artifact.get("runs", [])
        for item in run.get("cases", [])
        if item.get("id") in CASE_BY_ID
    ]


def _score_item(item: dict, scorer: str, *, pre_verify: bool = False) -> bool:
    case: Case = CASE_BY_ID[item["id"]]
    if pre_verify:
        if "answer_pre_verify" not in item:
            return bool(item.get("pass_pre_verify", item.get("pass", False)))
        answer = item.get("answer_pre_verify")
        text = item.get("final_text_pre_verify", "")
        ok = bool(str(text).strip()) and str(text).strip() != BUDGET_EXHAUSTED_SENTINEL
    else:
        answer = item.get("answer")
        text = item.get("final_text", "")
        ok = bool(item.get("ok", True))
    passed, _ = _score(case, answer, text, ok, scorer=scorer)
    return passed


def _rate(items: list[dict], scorer: str, pre_verify: bool = False) -> float:
    if not items:
        return 0.0
    return sum(_score_item(item, scorer, pre_verify=pre_verify) for item in items) / len(items)


def _repeat_rates(items: list[dict], scorer: str) -> list[float]:
    by_repeat: dict[int, list[dict]] = {}
    for item in items:
        by_repeat.setdefault(int(item.get("repeat", 0)), []).append(item)
    return [
        _rate(by_repeat[repeat], scorer)
        for repeat in sorted(by_repeat)
    ]


def _group(items: list[dict], scorer: str, key: str) -> dict[str, float]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(str(item.get(key, "?")), []).append(item)
    return {name: _rate(group, scorer) for name, group in sorted(groups.items())}


def _mcnemar_exact(wins: int, losses: int) -> float:
    """Two-sided exact binomial p for discordant task-level wins/losses."""
    discordant = wins + losses
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, i) for i in range(min(wins, losses) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def _bootstrap(
    task_differences: list[float],
    *,
    iterations: int = 5000,
    seed: int = 20261114,
) -> dict[str, Any]:
    if not task_differences:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "iterations": iterations}
    rng = random.Random(seed)
    n = len(task_differences)
    means: list[float] = []
    for _ in range(iterations):
        sample = [task_differences[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    low = means[int(0.025 * (len(means) - 1))]
    high = means[int(0.975 * (len(means) - 1))]
    return {
        "mean": sum(task_differences) / n,
        "ci_low": low,
        "ci_high": high,
        "iterations": iterations,
        "seed": seed,
    }


def _task_level(single_items: list[dict], multi_items: list[dict], scorer: str) -> dict:
    single_by_id: dict[int, list[dict]] = {}
    multi_by_id: dict[int, list[dict]] = {}
    for item in single_items:
        single_by_id.setdefault(item["id"], []).append(item)
    for item in multi_items:
        multi_by_id.setdefault(item["id"], []).append(item)

    tasks = []
    differences: list[float] = []
    wins = losses = ties = 0
    for case_id in sorted(set(single_by_id) & set(multi_by_id)):
        single_rate = _rate(single_by_id[case_id], scorer)
        multi_rate = _rate(multi_by_id[case_id], scorer)
        difference = multi_rate - single_rate
        differences.append(difference)
        if difference > 0:
            wins += 1
            outcome = "multi_win"
        elif difference < 0:
            losses += 1
            outcome = "multi_loss"
        else:
            ties += 1
            outcome = "tie"
        tasks.append(
            {
                "id": case_id,
                "question": CASE_BY_ID[case_id].question,
                "single_rate": single_rate,
                "multi_rate": multi_rate,
                "difference": difference,
                "outcome": outcome,
                "single_repeats": len(single_by_id[case_id]),
                "multi_repeats": len(multi_by_id[case_id]),
            }
        )
    return {
        "tasks": tasks,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "discordant": wins + losses,
        "mcnemar_exact_p": _mcnemar_exact(wins, losses),
        "bootstrap": _bootstrap(differences),
    }


def _verifier_audit(multi_items: list[dict], scorer: str) -> dict:
    if not any("answer_pre_verify" in item for item in multi_items):
        return {"available": False, "reason": "artifact has no answer_pre_verify"}
    counts = {"wrong_to_right": 0, "right_to_wrong": 0, "right_to_right": 0, "wrong_to_wrong": 0}
    changed = 0
    pre_passed = 0
    final_passed = 0
    for item in multi_items:
        before = _score_item(item, scorer, pre_verify=True)
        after = _score_item(item, scorer)
        pre_passed += int(before)
        final_passed += int(after)
        changed += int(bool(item.get("verifier_changed_answer", False)))
        if before and after:
            counts["right_to_right"] += 1
        elif before and not after:
            counts["right_to_wrong"] += 1
        elif not before and after:
            counts["wrong_to_right"] += 1
        else:
            counts["wrong_to_wrong"] += 1
    return {
        "available": True,
        **counts,
        "changed_answer": changed,
        "pre_verify_passed": pre_passed,
        "final_passed": final_passed,
        "total": len(multi_items),
    }


def _arm_summary(artifact: dict, scorer: str, path: str | Path) -> dict:
    items = _case_runs(artifact)
    repeat_rates = _repeat_rates(items, scorer)
    return {
        "path": str(_resolve(path)),
        "arm": artifact.get("arm"),
        "model": artifact.get("model"),
        "prompt_sha256": artifact.get("prompt_sha256"),
        "multi_prompts_sha256": artifact.get("multi_prompts_sha256"),
        "scorer_version": artifact.get("scorer_version"),
        "verifier_mode": artifact.get("verifier_mode"),
        "repeats": artifact.get("repeats"),
        "case_runs": len(items),
        "mean_pass_rate": sum(repeat_rates) / len(repeat_rates) if repeat_rates else 0.0,
        "min_pass_rate": min(repeat_rates) if repeat_rates else 0.0,
        "max_pass_rate": max(repeat_rates) if repeat_rates else 0.0,
        "pass_v1": _rate(items, "v1"),
        "pass_v2": _rate(items, "v2"),
        "semantic_pass": _rate(items, "semantic"),
        "holdout": _rate([item for item in items if item.get("holdout")], scorer),
        "non_holdout": _rate([item for item in items if not item.get("holdout")], scorer),
        "by_category": _group(items, scorer, "category"),
        "by_difficulty": _group(items, scorer, "difficulty"),
        "tokens": sum(int(item.get("tokens", 0)) for item in items),
        "seconds": sum(float(item.get("seconds", 0.0)) for item in items),
        "calls": sum(int(item.get("calls", item.get("steps", 0))) for item in items),
        "mean_tokens_per_case": sum(int(item.get("tokens", 0)) for item in items)
        / max(len(items), 1),
        "mean_seconds_per_case": sum(float(item.get("seconds", 0.0)) for item in items)
        / max(len(items), 1),
        "mean_calls_per_case": sum(
            int(item.get("calls", item.get("steps", 0))) for item in items
        )
        / max(len(items), 1),
        "stalls": sum(int(item.get("stall_count", 0)) for item in items),
    }


def _print_table(single: dict, multi: dict, task: dict, primary: str) -> None:
    print(f"\nArm summary (primary scorer: {primary}):")
    print(f"{'arm':<8} {'cases':>5} {'mean':>6} {'min-max':>10} {'v1':>6} {'v2':>6} {'sem':>6} {'hold':>6} {'non-hold':>8}")
    for name, arm in (("single", single), ("multi", multi)):
        print(
            f"{name:<8} {arm['case_runs']:>5} {arm['mean_pass_rate']:>6.1%} "
            f"{arm['min_pass_rate']:.1%}-{arm['max_pass_rate']:.1%} "
            f"{arm['pass_v1']:>6.1%} {arm['pass_v2']:>6.1%} "
            f"{arm['semantic_pass']:>6.1%} {arm['holdout']:>6.1%} "
            f"{arm['non_holdout']:>8.1%}"
        )
    print(
        f"\nCost per case: multi/single tokens "
        f"{multi['mean_tokens_per_case'] / max(single['mean_tokens_per_case'], 1e-9):.2f}x, "
        f"seconds {multi['mean_seconds_per_case'] / max(single['mean_seconds_per_case'], 1e-9):.2f}x, "
        f"calls {multi['mean_calls_per_case'] / max(single['mean_calls_per_case'], 1e-9):.2f}x"
    )
    print("\nTask-level majority (repeats are not independent):")
    print(
        f"wins={task['wins']} losses={task['losses']} ties={task['ties']} "
        f"discordant={task['discordant']} exact McNemar p={task['mcnemar_exact_p']:.4f}"
    )
    boot = task["bootstrap"]
    print(
        f"paired task bootstrap mean diff={boot['mean']:+.3f} "
        f"95% CI [{boot['ci_low']:+.3f}, {boot['ci_high']:+.3f}] "
        f"({boot['iterations']} resamples)"
    )
    print("\nPer-task:")
    print(f"{'id':>3} {'single':>8} {'multi':>8} {'diff':>7}  outcome")
    for row in task["tasks"]:
        print(
            f"{row['id']:>3} {row['single_rate']:>8.0%} {row['multi_rate']:>8.0%} "
            f"{row['difference']:>+7.0%}  {row['outcome']}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single", required=True, help="single-arm run dir or run.json")
    parser.add_argument("--multi", required=True, help="multi-arm run dir or run.json")
    parser.add_argument("--scorer", choices=SCORER_VERSIONS, default="v2")
    parser.add_argument("--out", default=None, help="write comparison JSON here")
    args = parser.parse_args(argv)

    single_artifact = _load(args.single)
    multi_artifact = _load(args.multi)
    single_items = _case_runs(single_artifact)
    multi_items = _case_runs(multi_artifact)
    if not single_items or not multi_items:
        print("one arm has no cases", file=sys.stderr)
        return 2

    single = _arm_summary(single_artifact, args.scorer, args.single)
    multi = _arm_summary(multi_artifact, args.scorer, args.multi)
    task = _task_level(single_items, multi_items, args.scorer)
    audit = _verifier_audit(multi_items, args.scorer)

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "primary_scorer": args.scorer,
        "single": single,
        "multi": multi,
        "task_level": task,
        "verifier_audit": audit,
    }
    _print_table(single, multi, task, args.scorer)
    if audit.get("available"):
        print(
            "\nVerifier audit: "
            f"wrong->right {audit['wrong_to_right']} | "
            f"right->wrong {audit['right_to_wrong']} | "
            f"right->right {audit['right_to_right']} | "
            f"wrong->wrong {audit['wrong_to_wrong']} | "
            f"pre {audit['pre_verify_passed']}/{audit['total']} | "
            f"final {audit['final_passed']}/{audit['total']}"
        )
    else:
        print(f"\nVerifier audit: {audit.get('reason')}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nCOMPARISON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
