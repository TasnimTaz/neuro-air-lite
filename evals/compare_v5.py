"""Paired comparison of single vs multi runs, per model and pooled (paper Table 1 style).

    uv run python evals/compare_v5.py \\
        --pair runs/v5/qwen35b_single runs/v5/qwen35b_multi \\
        --pair runs/v5/dsflash_single  runs/v5/dsflash_multi

Reports, per pair and pooled: accuracy of each arm, gain in percentage points,
wins/losses (tasks solved by exactly one arm), exact two-sided McNemar p, and a
task-level cluster bootstrap 95% CI (tasks resampled, all models kept together,
which is what the paper does for the pooled estimate). Also: token ratio,
first-attempt execution success, and per-category accuracy.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from statistics import median

from scipy.stats import binomtest


def load_run(path: Path) -> dict[int, dict]:
    cases = json.loads((path / "run.json").read_text())["cases"]
    return {c["task_id"]: c for c in cases if not c.get("infra_failed")}


def mcnemar_exact(wins: int, losses: int) -> float:
    n = wins + losses
    return 1.0 if n == 0 else float(binomtest(wins, n, 0.5).pvalue)


def paired(single: dict[int, dict], multi: dict[int, dict]) -> list[tuple[int, int, int]]:
    """[(task_id, single_pass, multi_pass)] over tasks scored in BOTH arms."""
    return [(t, int(single[t]["pass"]), int(multi[t]["pass"])) for t in sorted(set(single) & set(multi))]


def stats(rows: list[tuple[int, int, int]]) -> dict:
    n = len(rows)
    s = sum(r[1] for r in rows)
    m = sum(r[2] for r in rows)
    wins = sum(1 for _, a, b in rows if b == 1 and a == 0)
    losses = sum(1 for _, a, b in rows if a == 1 and b == 0)
    return {"n": n, "single": s, "multi": m, "gain_pp": round(100 * (m - s) / n, 1) if n else None,
            "wins": wins, "losses": losses, "mcnemar_p": round(mcnemar_exact(wins, losses), 4)}


def cluster_bootstrap(per_model_rows: list[list[tuple[int, int, int]]], b: int = 10000, seed: int = 20261122):
    """Resample task ids; keep every model's pair for a drawn task (paper's pooled CI)."""
    by_task: dict[int, list[tuple[int, int]]] = {}
    for rows in per_model_rows:
        for t, a, m in rows:
            by_task.setdefault(t, []).append((a, m))
    ids = sorted(by_task)
    rng = random.Random(seed)
    diffs = []
    for _ in range(b):
        tot = d = 0
        for t in (rng.choice(ids) for _ in ids):
            for a, m in by_task[t]:
                tot += 1
                d += m - a
        diffs.append(100 * d / tot)
    diffs.sort()
    return round(diffs[int(0.025 * b)], 1), round(diffs[int(0.975 * b)], 1)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pair", nargs=2, action="append", required=True, metavar=("SINGLE_DIR", "MULTI_DIR"))
    args = ap.parse_args(argv)

    all_rows, report = [], []
    for sdir, mdir in args.pair:
        single, multi = load_run(Path(sdir)), load_run(Path(mdir))
        rows = paired(single, multi)
        st = stats(rows)
        common = sorted(set(single) & set(multi))
        st["token_ratio_median"] = round(median(multi[t]["tokens"] for t in common) / max(1, median(single[t]["tokens"] for t in common)), 2) if common else None
        st["first_attempt_single"] = round(sum(1 for t in common if single[t].get("first_attempt_ok")) / max(1, len(common)), 3)
        st["first_attempt_multi"] = round(sum(1 for t in common if multi[t].get("first_attempt_ok")) / max(1, len(common)), 3)
        cats: dict[str, list[int]] = {}
        for t in common:
            c = single[t]["category"]
            cats.setdefault(c, [0, 0, 0])
            cats[c][0] += 1
            cats[c][1] += int(single[t]["pass"])
            cats[c][2] += int(multi[t]["pass"])
        st["by_category(single/multi of n)"] = {k: f"{v[1]}/{v[2]} of {v[0]}" for k, v in sorted(cats.items())}
        st["pair"] = [sdir, mdir]
        report.append(st)
        all_rows.append(rows)

    flat = [r for rows in all_rows for r in rows]
    pooled = stats(flat)
    pooled["bootstrap95_pp"] = cluster_bootstrap(all_rows) if flat else None
    print(json.dumps({"per_pair": report, "pooled": pooled}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
