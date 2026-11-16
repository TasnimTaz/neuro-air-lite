"""Validate or run the ten premise traps.

The default mode validates the JSON and prints a summary. It is safe without
Ollama or the database:

    uv run python scripts/check_premise_traps.py

The live mode runs the single-agent baseline on each trap after the evaluation
freeze. It writes one JSON per trap and a run artifact for manual review; it is
not part of the numeric benchmark and does not change the frozen prompt:

    uv run python scripts/check_premise_traps.py --live \
        --out runs/premise_traps/day8_single

Each query is judged against its ``expected_behaviour`` (halt, ask clarification
or state limitation) by reading the recorded final text and ANSWER. The script
does not pretend a keyword matcher is a complete judge, so it records the raw
result and leaves the qualitative decision visible.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _prompt_sha256() -> str:
    """Hash the assembled evaluated prompt through the read-only runtime."""
    from domains.hk.schema_prompt import get_system_prompt
    from domains.hk.tools import build_runtime
    from evals.run_baseline import PROMPT_TIME

    runtime = build_runtime()
    try:
        preview = get_system_prompt(engine=runtime.ns["engine"], now=PROMPT_TIME)
    finally:
        runtime.close()
    return hashlib.sha256(preview.encode("utf-8")).hexdigest()


def _dummy_case(trap: dict[str, object], position: int):
    """Wrap a trap as a Case so the frozen run_case loop can execute it.

    The dummy truth query and pinned value are only there to satisfy run_case's
    integrity check. The trap's outcome is reviewed from final_text and ANSWER,
    never from the numeric pass flag.
    """
    from evals.hk_baseline_v2 import Case

    return Case(
        id=position,
        category="premise",
        difficulty="trap",
        question=str(trap["query"]),
        truth_sql="SELECT 1",
        expected=1,
        kind="int",
        note=f"expected_behaviour={trap['expected_behaviour']}: {trap.get('why', '')}",
    )


async def _run_live(args: argparse.Namespace, traps: list[dict[str, object]]) -> int:
    from core.env import admin_url, load_agent_env
    from domains.hk.db import make_admin_engine
    from evals.run_baseline import run_case

    dsn = admin_url()
    load_agent_env(override=False, scrub=True)
    if "DB_URL_ADMIN" in os.environ:
        print(
            "[FAIL] DB_URL_ADMIN is exported; the agent environment is not scrubbed",
            file=sys.stderr,
        )
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (
        Path(args.resume)
        if args.resume
        else Path(args.out)
        if args.out
        else ROOT / "runs" / "premise_traps" / stamp
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt_sha256 = _prompt_sha256()
    meta = {
        "kind": "premise_traps",
        "arm": args.arm,
        "verifier_mode": args.verifier_mode,
        "model": os.environ.get("LLM_MODEL"),
        "base_url": os.environ.get("LLM_BASE_URL"),
        "prompt_sha256": prompt_sha256,
        "max_steps": args.max_steps,
        "data": str(args.data),
        "traps": len(traps),
        "resumed": bool(args.resume),
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(out_dir / "run_meta.json", meta)
    print(f"premise traps: {len(traps)} | arm={args.arm} | prompt_sha256={prompt_sha256}")
    print(f"artifact: {out_dir}")

    admin = make_admin_engine(dsn)
    outcomes: list[dict[str, object]] = []
    try:
        for position, trap in enumerate(traps, start=1):
            trap_id = str(trap["id"])
            case_path = out_dir / "cases" / f"{trap_id}.json"
            if args.resume and case_path.exists():
                outcome = json.loads(case_path.read_text(encoding="utf-8"))
                status = "resumed"
            else:
                try:
                    outcome = await run_case(
                        _dummy_case(trap, position),
                        admin,
                        args.max_steps,
                        False,
                        arm=args.arm,
                        verifier_mode=args.verifier_mode,
                    )
                except Exception as error:  # noqa: BLE001
                    outcome = {
                        "id": position,
                        "ok": False,
                        "error": f"{type(error).__name__}: {error}",
                        "final_text": "",
                        "answer": None,
                    }
                outcome["trap_id"] = trap_id
                outcome["query"] = trap["query"]
                outcome["expected_behaviour"] = trap["expected_behaviour"]
                outcome["why"] = trap.get("why", "")
                _write_json(case_path, outcome)
                status = "ran"
            outcomes.append(outcome)

            final_text = str(outcome.get("final_text") or outcome.get("error") or "")
            print(
                f"[{position}/{len(traps)}] {trap_id} {status} "
                f"expected={trap['expected_behaviour']} "
                f"answer={outcome.get('answer')!r} "
                f"steps={outcome.get('steps')} sec={outcome.get('seconds')}"
            )
            print("  final: " + final_text.replace("\n", "\n         ")[:1200])

        counts: dict[str, int] = {}
        for trap in traps:
            behaviour = str(trap["expected_behaviour"])
            counts[behaviour] = counts.get(behaviour, 0) + 1
        run = {
            **meta,
            "finished": datetime.datetime.now().isoformat(timespec="seconds"),
            "expected_counts": counts,
            "cases": outcomes,
        }
        _write_json(out_dir / "run.json", run)
        print(f"wrote {out_dir / 'run.json'}")
    finally:
        admin.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the single-agent baseline on every trap (Ollama + database)",
    )
    parser.add_argument("--arm", choices=("single", "multi"), default="single")
    parser.add_argument(
        "--verifier-mode",
        choices=("overwrite", "flag", "none"),
        default="overwrite",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="resume an existing premise-trap run directory",
    )
    args = parser.parse_args()
    if args.out and args.resume:
        parser.error("--out and --resume are mutually exclusive")

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:  # pragma: no cover - older/embedded streams
            pass

    traps = load_traps(args.data)
    counts: dict[str, int] = {}
    for trap in traps:
        behaviour = str(trap["expected_behaviour"])
        counts[behaviour] = counts.get(behaviour, 0) + 1

    print(f"validated {len(traps)} premise traps from {args.data}")
    for behaviour in sorted(counts):
        print(f"  {behaviour}: {counts[behaviour]}")
    if args.live:
        return asyncio.run(_run_live(args, traps))
    print("dry-run only: no LLM call, no database call")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
