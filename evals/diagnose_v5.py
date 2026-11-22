"""Explain a single-vs-multi gap from two v5 run folders (no DB, no model needed).

    uv run python evals/diagnose_v5.py runs/v5/dev_single runs/v5/dev_multi

Reads run.json (or, if absent, cases/*.json). The field names below match what
evals/run_v5.py from the v5 pack writes. If your harness wrote different names, the
schema preflight says which fields are missing and which sections are skipped; nothing
crashes on a missing optional field.

Sections: 1 headroom, 2 cause of each multi loss, 3 stalls (association, NOT causation),
4 multi without its verifier, 5 accuracy by category/difficulty.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

STALL_PREFIX = "I stopped because the identical code block"
REQUIRED = ("task_id", "pass")
OPTIONAL = ("category", "difficulty", "validator", "pass_pre_verify", "stall_count", "worker_runs",
            "verifier_changed", "verifier_rejections", "budget_exhausted", "last_error", "steps", "infra_failed")


def load(path: Path) -> dict[int, dict]:
    run = path / "run.json"
    if run.exists():
        cases = json.loads(run.read_text(encoding="utf-8")).get("cases")
        if cases is None:
            raise SystemExit(f"{run} has no 'cases' list; keys are {list(json.loads(run.read_text()).keys())}")
    else:
        files = sorted((path / "cases").glob("*.json"))
        if not files:
            raise SystemExit(f"neither {run} nor {path / 'cases'}/*.json found")
        cases = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    scored = [c for c in cases if not c.get("infra_failed")]
    if not scored:
        raise SystemExit(f"{path}: every case is infra_failed; nothing to analyse")
    seen = Counter(c["task_id"] for c in scored)
    repeated = sorted(t for t, n in seen.items() if n > 1)
    if repeated:
        raise SystemExit(
            f"{path}: {len(repeated)} task_id(s) appear more than once (e.g. {repeated[:5]}); this script assumes "
            "one entry per task. Aggregate the repeats first (e.g. majority pass) or key by (task_id, repeat)."
        )
    return {c["task_id"]: c for c in scored}


def preflight(name: str, runs: dict[int, dict]) -> set[str]:
    first = next(iter(runs.values()))
    print(f"[schema] {name}: {len(runs)} scored cases; keys of first case: {sorted(first)}")
    missing_req = [k for k in REQUIRED if k not in first]
    if missing_req:
        raise SystemExit(f"[schema] {name}: required field(s) missing: {missing_req}. Adapt this script to your run.json.")
    present = {k for k in OPTIONAL if any(k in c for c in runs.values())}
    absent = [k for k in OPTIONAL if k not in present]
    if absent:
        print(f"[schema] {name}: optional fields absent (related sections degrade): {absent}")
    return present


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "0/0"


def checks(m: dict) -> list[dict]:
    return (m.get("validator") or {}).get("checks") or []


def loss_cause(m: dict) -> str:
    if m.get("budget_exhausted"):
        return "budget exhausted"
    if m.get("pass_pre_verify") and not m["pass"]:
        return "verifier broke a correct value"
    if any(c.get("reason") == "variable not set" for c in checks(m)):
        return "required variable never staged"
    if "pass_pre_verify" in m and not m["pass_pre_verify"]:
        return "wrong value before verifier (upstream/synthesis)"
    return "unknown (fields needed: pass_pre_verify, validator.checks)"


def stall_roles(m: dict) -> list[str]:
    return [w.get("role", "?") for w in m.get("worker_runs") or [] if (w.get("final_text") or "").startswith(STALL_PREFIX)]


def main(single_dir: str, multi_dir: str) -> int:
    S, M = load(Path(single_dir)), load(Path(multi_dir))
    pre_s, pre_m = preflight("single", S), preflight("multi", M)
    ids = sorted(set(S) & set(M))
    if not ids:
        raise SystemExit("no task ids in common between the two runs")
    if len(ids) != len(S) or len(ids) != len(M):
        print(f"[warn] arms differ in scored tasks: single={len(S)} multi={len(M)} common={len(ids)} (analysis uses common only)")
    wins = [t for t in ids if M[t]["pass"] and not S[t]["pass"]]
    losses = [t for t in ids if S[t]["pass"] and not M[t]["pass"]]

    print(f"\n== 1. Headroom (n={len(ids)})")
    sp = sum(bool(S[t]["pass"]) for t in ids)
    print(f"single {pct(sp, len(ids))}; multi {pct(sum(bool(M[t]['pass']) for t in ids), len(ids))}")
    sf = len(ids) - sp
    print(f"multi can win at most {sf} (single failures) and lose up to {sp} (single passes); observed wins={len(wins)} losses={len(losses)}")
    if sp / len(ids) > 0.9:
        print("[ceiling] single > 90%: this set cannot show a multi-agent gain whatever multi does; treat as uninformative for the gain question")

    print("\n== 2. Why multi lost")
    for k, v in Counter(loss_cause(M[t]) for t in losses).most_common():
        print(f"  {v:>3}  {k}")
    for t in losses:
        m = M[t]
        bad = [f"{c.get('var', '?')}:{c.get('reason', '?')}" for c in checks(m) if not c.get("ok", True)]
        print(f"   task {t:>3} [{m.get('category', '?')}/{m.get('difficulty', '?')}] cause={loss_cause(m)} "
              f"stalls={m.get('stall_count', '?')} steps={m.get('steps', '?')} verifier_changed={m.get('verifier_changed', '?')} bad={bad}")
        if m.get("last_error"):
            print(f"        last_error: {str(m['last_error'])[:160]!r}")

    print("\n== 3. Stalls (an ASSOCIATION; a hard task can cause both stalls and failure)")
    if "stall_count" in pre_m:
        with_s = [t for t in ids if M[t].get("stall_count", 0) > 0]
        without = [t for t in ids if M[t].get("stall_count", 0) == 0]
        print(f"  multi pass with >=1 stall: {pct(sum(bool(M[t]['pass']) for t in with_s), len(with_s))}   without: {pct(sum(bool(M[t]['pass']) for t in without), len(without))}")
        print(f"  CONTROL: single pass on the same stalled tasks: {pct(sum(bool(S[t]['pass']) for t in with_s), len(with_s))}")
        print("  Read it this way: if single ALSO fails the stalled tasks, stalls mark hard tasks. If single passes them and multi fails, stalls point at the multi harness.")
        print(f"  stalls by role: {dict(Counter(r for t in ids for r in stall_roles(M[t])))}")
        unset = [t for t in with_s if any(c.get('reason') == 'variable not set' for c in checks(M[t]))]
        print(f"  stalled tasks that ended with a required variable unset: {len(unset)}")
    else:
        print("  skipped: no stall_count field")

    print("\n== 4. Topology vs verifier")
    if "pass_pre_verify" in pre_m:
        pre = sum(1 for t in ids if M[t].get("pass_pre_verify"))
        rescued = sum(1 for t in ids if not M[t].get("pass_pre_verify") and M[t]["pass"])
        broken = sum(1 for t in ids if M[t].get("pass_pre_verify") and not M[t]["pass"])
        print(f"  single {pct(sp, len(ids))} | multi pre-verifier {pct(pre, len(ids))} | multi final {pct(sum(bool(M[t]['pass']) for t in ids), len(ids))}")
        print(f"  verifier rescued {rescued}, broke {broken}; safe-mode rejections: {sum(len(M[t].get('verifier_rejections') or []) for t in ids)}")
    else:
        print("  skipped: no pass_pre_verify field")

    print("\n== 5. Accuracy by category / difficulty (single | multi)")
    for key in ("category", "difficulty"):
        g = defaultdict(lambda: [0, 0, 0])
        for t in ids:
            k = S[t].get(key) or M[t].get(key) or "?"
            g[k][0] += 1
            g[k][1] += int(bool(S[t]["pass"]))
            g[k][2] += int(bool(M[t]["pass"]))
        for k, (n, s, m) in sorted(g.items()):
            print(f"  {key:<10} {k:<12} n={n:<3} single {s}/{n}  multi {m}/{n}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
