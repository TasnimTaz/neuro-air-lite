"""Export frozen v5 case artifacts to compact, auditable CSV files.

Generated, never hand-edited:

    uv run python scripts/export_case_csv.py

The input is the per-case JSON under ``runs/v5/.../cases/``. The resulting CSVs
allow a reviewer to re-derive the headline pass counts and the paired verifier
accounting without loading the raw run folders.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

FIELDS = [
    "split",
    "arm",
    "task_id",
    "category",
    "difficulty",
    "pass",
    "pass_pre_verify",
    "tokens",
    "seconds",
    "stall_count",
    "verifier_changed",
    "infra_failed",
    "case_file",
]

RUNS = {
    "dev": ("runs/v5/qwen14b_dev_single", "runs/v5/qwen14b_dev_multi", "docs/results/v5_dev_cases.csv"),
    "test": ("runs/v5/qwen14b_test_single", "runs/v5/qwen14b_test_multi", "docs/results/v5_test_cases.csv"),
}


def _cell(value: Any) -> str:
    """Stable CSV cell for booleans, numbers and missing values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _read_cases(run_dir: Path, split: str, arm: str) -> list[dict[str, str]]:
    case_dir = run_dir / "cases"
    paths = sorted(case_dir.glob("task-*.json")) + sorted(case_dir.glob("repeat-*-case-*.json"))
    if not paths:
        raise RuntimeError(f"no case files under {case_dir}")
    rows: list[dict[str, str]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "split": split,
                "arm": arm,
                "task_id": _cell(data.get("task_id")),
                "category": _cell(data.get("category")),
                "difficulty": _cell(data.get("difficulty")),
                "pass": _cell(bool(data.get("pass"))),
                "pass_pre_verify": _cell(data.get("pass_pre_verify")),
                "tokens": _cell(int(data.get("tokens") or 0)),
                "seconds": _cell(round(float(data.get("seconds") or 0.0), 3)),
                "stall_count": _cell(data.get("stall_count")),
                "verifier_changed": _cell(data.get("verifier_changed")),
                "infra_failed": _cell(bool(data.get("infra_failed"))),
                "case_file": path.name,
            }
        )
    return rows


def export(split: str, single: str, multi: str, out_csv: str) -> Path:
    rows = _read_cases(ROOT / single, split, "single")
    rows += _read_cases(ROOT / multi, split, "multi")
    rows.sort(key=lambda r: (r["arm"], int(r["task_id"])))
    dest = ROOT / out_csv
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[csv] {out_csv} rows={len(rows)} single={sum(r['arm']=='single' for r in rows)} multi={sum(r['arm']=='multi' for r in rows)}")
    return dest


def main() -> int:
    for split, args in RUNS.items():
        export(split, *args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
