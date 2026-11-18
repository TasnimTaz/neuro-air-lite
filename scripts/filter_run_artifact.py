"""Filter a saved baseline artifact to a matched task subset.

This does not rescore or rewrite any case. It selects existing case records and
recomputes the same frozen summary, so a full 20-task single artifact can be
compared with a 14-task non-hold-out verifier artifact without mixing 60-case
and 42-case denominators.

Usage::

    uv run python scripts/filter_run_artifact.py \
        --input runs/baseline/day5b_v3_budget20_3x \
        --non-holdout \
        --out runs/single_verified/inputs/single_v3_nonholdout_3x

    uv run python evals/compare_arms.py \
        --single runs/single_verified/inputs/single_v3_nonholdout_3x \
        --multi runs/multi/day5b_v3_verifier_flag_nonholdout_3x \
        --out runs/multi/day5b_v3_verifier_flag_nonholdout_3x/compare_vs_single_matched.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.hk_baseline_v2 import CASES  # noqa: E402
from evals.run_baseline import _summary, _write_json  # noqa: E402


def _artifact_path(path: str | Path) -> Path:
    value = Path(path)
    return value / "run.json" if value.is_dir() else value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="artifact directory or run.json")
    parser.add_argument("--out", required=True, help="output artifact directory")
    parser.add_argument(
        "--non-holdout",
        action="store_true",
        help="keep only non-hold-out tasks",
    )
    parser.add_argument(
        "--only",
        type=int,
        nargs="*",
        default=None,
        help="explicit case ids; applied after --non-holdout",
    )
    args = parser.parse_args(argv)

    source = _artifact_path(args.input)
    artifact = json.loads(source.read_text(encoding="utf-8"))
    if "runs" not in artifact:
        raise SystemExit(f"not a baseline artifact: {source}")

    selected = tuple(
        case
        for case in CASES
        if (not args.non_holdout or not case.holdout)
        and (not args.only or case.id in args.only)
    )
    ids = {case.id for case in selected}
    if not ids:
        raise SystemExit("no cases selected")

    runs: list[dict] = []
    for run in artifact.get("runs", []):
        cases = [item for item in run.get("cases", []) if item.get("id") in ids]
        if not cases:
            continue
        runs.append(
            {
                "repeat": run.get("repeat"),
                "passed": sum(bool(item.get("pass")) for item in cases),
                "total": len(cases),
                "cases": cases,
            }
        )
    if not runs:
        raise SystemExit("no matching case records in artifact")

    repeats = int(artifact.get("repeats") or len(runs))
    summary = _summary(runs, repeats, selected)
    filtered = {
        **artifact,
        "source_artifact": str(source),
        "selected": [case.id for case in selected],
        "repeats": repeats,
        "summary": summary,
        "runs": runs,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "run.json", filtered)
    _write_json(out_dir / "run_meta.json", {k: v for k, v in filtered.items() if k not in {"runs", "summary"}})
    overall = summary["overall"]
    non_holdout = summary["non_holdout"]
    print(
        f"{out_dir / 'run.json'}: {overall['passed']}/{overall['total']} "
        f"case-runs; non-hold-out {non_holdout['passed']}/{non_holdout['total']} "
        f"({non_holdout['rate']:.1%}); repeats {repeats}; tasks {len(selected)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
