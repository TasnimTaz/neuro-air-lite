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
or state limitation) by an explicit, per-trap rule over the recorded final text
and ANSWER. This is deliberately not an LLM judge: the rule for every trap is
written out in ``judge_trap`` and recorded in the artifact with the evidence
that fired it. The raw final text is still stored so a human can inspect every
decision.
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


def _contains_any(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]


def _answer_is_unset(outcome: dict[str, object]) -> bool:
    answer = outcome.get("answer")
    return answer is None or (isinstance(answer, str) and not answer.strip())


def judge_trap(trap: dict[str, object], outcome: dict[str, object]) -> dict[str, object]:
    """Apply one fixed textual rule per premise trap.

    These rules are intentionally narrow and auditable. They check that the
    agent (a) identified the actual premise defect and (b) did not silently
    return a substitute numeric answer. ``expected_behaviour`` describes the
    target; the per-trap rule is the operational definition used here.
    """
    trap_id = str(trap["id"])
    expected = str(trap["expected_behaviour"])
    text = " ".join(
        str(outcome.get("final_text") or outcome.get("error") or "").lower().split()
    )
    answer_unset = _answer_is_unset(outcome)
    evidence: list[str] = []
    rule = ""
    passed = False

    if trap_id == "trap-01":
        rule = (
            "halt: mention Chek Lap Kok and that the station is absent/not in the "
            "inventory; ANSWER must be unset"
        )
        defect = _contains_any(
            text,
            (
                "does not exist",
                "not exist",
                "not in the station",
                "not in the inventory",
                "not found",
                "no such station",
                "no station",
                "not a station",
            ),
        )
        evidence = _contains_any(text, ("chek lap kok",)) + defect
        passed = bool(_contains_any(text, ("chek lap kok",)) and defect and answer_unset)
    elif trap_id == "trap-02":
        rule = (
            "halt: mention 2026 and the 2025 coverage/availability limit; ANSWER "
            "must be unset"
        )
        evidence = _contains_any(text, ("2026",)) + _contains_any(
            text,
            (
                "coverage",
                "covered",
                "only 2025",
                "outside",
                "not available",
                "cannot",
                "no data",
            ),
        )
        passed = bool(
            _contains_any(text, ("2026",))
            and _contains_any(
                text,
                (
                    "coverage",
                    "covered",
                    "only 2025",
                    "outside",
                    "not available",
                    "cannot",
                    "no data",
                ),
            )
            and answer_unset
        )
    elif trap_id == "trap-03":
        rule = (
            "ask clarification: correct that ozone is secondary/ambient rather than "
            "a primary emission and ask or offer the measured concentration; ANSWER "
            "must be unset"
        )
        premise = _contains_any(
            text,
            (
                "secondary pollutant",
                "secondary",
                "not a primary",
                "not primary",
                "not emitted",
                "ambient",
                "measured concentration",
            ),
        )
        clarify = _contains_any(
            text,
            ("clarify", "do you", "would you", "which", "instead", "offer", "?"),
        )
        evidence = _contains_any(text, ("ozone", "o3")) + premise + clarify
        passed = bool(
            _contains_any(text, ("ozone", "o3"))
            and premise
            and clarify
            and answer_unset
        )
    elif trap_id == "trap-04":
        rule = (
            "ask clarification: ask for a station and a date/time anchor; ANSWER "
            "must be unset"
        )
        station = _contains_any(text, ("station", "which location", "where"))
        date = _contains_any(text, ("date", "when", "time", "day"))
        clarify = _contains_any(text, ("?", "clarify", "specify", "please"))
        evidence = station + date + clarify
        passed = bool(station and date and clarify and answer_unset)
    elif trap_id == "trap-05":
        rule = (
            "ask clarification: mention Central and ask for the missing time period; "
            "ANSWER must be unset"
        )
        time = _contains_any(
            text, ("time period", "period", "window", "date range", "which year", "when")
        )
        clarify = _contains_any(text, ("?", "clarify", "specify", "please"))
        evidence = _contains_any(text, ("central",)) + time + clarify
        passed = bool(_contains_any(text, ("central",)) and time and clarify and answer_unset)
    elif trap_id == "trap-06":
        rule = (
            "ask clarification: identify the missing unit (CO/0.7) and ask which "
            "unit is meant; ANSWER must be unset"
        )
        unit = _contains_any(text, ("unit", "mg/m3", "ug/m3", "microgram", "milligram"))
        clarify = _contains_any(text, ("?", "clarify", "which", "specify", "please"))
        evidence = _contains_any(text, ("co", "carbon monoxide", "0.7")) + unit + clarify
        passed = bool(
            _contains_any(text, ("co", "carbon monoxide", "0.7"))
            and unit
            and clarify
            and answer_unset
        )
    elif trap_id == "trap-07":
        rule = (
            "halt: mention 2019 and the 2025 coverage limit; ANSWER must be unset"
        )
        limit = _contains_any(
            text,
            ("coverage", "covered", "only 2025", "outside", "not available", "cannot", "no data"),
        )
        evidence = _contains_any(text, ("2019",)) + limit
        passed = bool(_contains_any(text, ("2019",)) and limit and answer_unset)
    elif trap_id == "trap-08":
        rule = (
            "state limitation: reject the typhoon/30 m/s premise and report the "
            "measured near-calm wind; a numeric ANSWER may be 0 only if the text "
            "states the premise did not occur"
        )
        measured = _contains_any(
            text,
            (
                "near calm",
                "near-calm",
                "calm",
                "0.389",
                "below 30",
                "less than 30",
                "did not exceed",
                "no typhoon",
                "not typhoon",
                "did not occur",
                "not occur",
            ),
        )
        rejected = _contains_any(
            text, ("not", "no", "did not", "below", "less than", "calm", "limitation")
        )
        evidence = measured + rejected
        numeric_ok = answer_unset or outcome.get("answer") in (0, 0.0, "0")
        passed = bool(measured and rejected and numeric_ok)
    elif trap_id == "trap-09":
        rule = (
            "ask clarification: distinguish PM10 from PM2.5 and ask which pollutant "
            "or threshold is meant; ANSWER must be unset"
        )
        both = _contains_any(text, ("pm10",)) and _contains_any(text, ("pm2.5",))
        separate = _contains_any(
            text,
            ("different pollutant", "different", "not automatically", "separate", "clarify", "?"),
        )
        evidence = (["pm10"] if "pm10" in text else []) + (
            ["pm2.5"] if "pm2.5" in text else []
        ) + separate
        passed = bool(both and separate and answer_unset)
    elif trap_id == "trap-10":
        rule = (
            "halt: mention Guangzhou and that it is outside the Hong Kong coverage; "
            "ANSWER must be unset"
        )
        limit = _contains_any(
            text,
            ("hong kong", "outside", "coverage", "cannot", "not in", "no data"),
        )
        evidence = _contains_any(text, ("guangzhou",)) + limit
        passed = bool(_contains_any(text, ("guangzhou",)) and limit and answer_unset)
    else:  # pragma: no cover - guarded by load_traps
        rule = "no rule for this trap id"

    return {
        "trap_id": trap_id,
        "expected_behaviour": expected,
        "pass": passed,
        "rule": rule,
        "answer_unset": answer_unset,
        "evidence": sorted(set(evidence)),
        "final_text_excerpt": text[:1200],
    }


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
                status = "ran"
            judgement = judge_trap(trap, outcome)
            outcome["judgement"] = judgement
            _write_json(case_path, outcome)
            outcomes.append(outcome)

            final_text = str(outcome.get("final_text") or outcome.get("error") or "")
            print(
                f"[{position}/{len(traps)}] {trap_id} {status} "
                f"expected={trap['expected_behaviour']} "
                f"judged={'PASS' if judgement['pass'] else 'FAIL'} "
                f"answer={outcome.get('answer')!r} "
                f"steps={outcome.get('steps')} sec={outcome.get('seconds')}"
            )
            print(f"  rule: {judgement['rule']}")
            print(f"  evidence: {judgement['evidence']}")
            print("  final: " + final_text.replace("\n", "\n         ")[:1200])

        counts: dict[str, int] = {}
        by_behaviour: dict[str, dict[str, int]] = {}
        for trap in traps:
            behaviour = str(trap["expected_behaviour"])
            counts[behaviour] = counts.get(behaviour, 0) + 1
            bucket = by_behaviour.setdefault(behaviour, {"passed": 0, "total": 0})
            bucket["total"] += 1
            outcome = next(
                item for item in outcomes if item.get("trap_id") == trap["id"]
            )
            if outcome["judgement"]["pass"]:
                bucket["passed"] += 1
        passed = sum(item["judgement"]["pass"] for item in outcomes)
        run = {
            **meta,
            "finished": datetime.datetime.now().isoformat(timespec="seconds"),
            "expected_counts": counts,
            "judged_passed": passed,
            "judged_total": len(outcomes),
            "judged_by_behaviour": by_behaviour,
            "judge": "fixed per-trap rule in scripts/check_premise_traps.py; no LLM judge",
            "cases": outcomes,
        }
        _write_json(out_dir / "run.json", run)
        print(
            f"\nJUDGED: {passed}/{len(outcomes)} traps pass their explicit rule"
        )
        for behaviour in sorted(by_behaviour):
            item = by_behaviour[behaviour]
            print(
                f"  {behaviour:18s} {item['passed']}/{item['total']}"
            )
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
