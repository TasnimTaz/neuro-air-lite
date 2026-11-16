"""Stub runner for the premise-trap set.

Day 6 writes the trap file and this runner only. It deliberately does not call
the LLM: the live premise run happens after the background verifier-mode and
stronger-model evaluations finish, and it needs a separate report. The default
mode validates the JSON and prints a summary, which is safe to run without
Ollama or the database.

    uv run python scripts/check_premise_traps.py
    # Later, after the eval freeze:
    uv run python scripts/check_premise_traps.py --live   # not implemented yet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "evals" / "premise_traps.json"
EXPECTED = {"halt", "ask clarification", "state limitation"}


def load_traps(path: str | Path = DEFAULT_DATA) -> list[dict[str, object]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("premise-trap file must be a JSON object")
    traps = payload.get("traps")
    if not isinstance(traps, list) or len(traps) != 10:
        raise ValueError("premise-trap file must contain exactly 10 traps")
    seen: set[str] = set()
    for trap in traps:
        if not isinstance(trap, dict):
            raise ValueError("each trap must be an object")
        trap_id = str(trap.get("id", "")).strip()
        query = str(trap.get("query", "")).strip()
        behaviour = str(trap.get("expected_behaviour", "")).strip()
        if not trap_id or trap_id in seen:
            raise ValueError(f"missing or duplicate trap id: {trap_id!r}")
        seen.add(trap_id)
        if not query:
            raise ValueError(f"trap {trap_id} has no query")
        if behaviour not in EXPECTED:
            raise ValueError(f"trap {trap_id} has an invalid expected_behaviour: {behaviour!r}")
    return traps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--live",
        action="store_true",
        help="reserved for the post-eval live runner; currently not implemented",
    )
    args = parser.parse_args()

    traps = load_traps(args.data)
    counts: dict[str, int] = {}
    for trap in traps:
        behaviour = str(trap["expected_behaviour"])
        counts[behaviour] = counts.get(behaviour, 0) + 1

    print(f"validated {len(traps)} premise traps from {args.data}")
    for behaviour in sorted(counts):
        print(f"  {behaviour}: {counts[behaviour]}")
    if args.live:
        print("live runner is not implemented; run it only after the eval freeze", file=sys.stderr)
        return 2
    print("dry-run only: no LLM call, no database call")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
