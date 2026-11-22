"""Budget watchdog for a long eval run. Reads the per-case JSON files the harness
writes (never edits them) and kills the run's process tree when a limit is hit.

    uv run python scripts/watch_run.py --run-dir runs/strong/single_x --pid 1234 \
        --max-tokens 4000000 --max-case-tokens 400000 --max-case-seconds 900
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def scan(run_dir: Path) -> dict:
    total = cases = 0
    worst_tokens = worst_seconds = 0.0
    failed_ok = 0
    newest = 0.0
    for path in (run_dir / "cases").glob("repeat-*-case-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "tokens" not in data or "executions" not in data:
            continue  # not a per-case artifact
        cases += 1
        newest = max(newest, path.stat().st_mtime)
        total += int(data.get("tokens") or 0)
        worst_tokens = max(worst_tokens, float(data.get("tokens") or 0))
        worst_seconds = max(worst_seconds, float(data.get("seconds") or 0))
        if data.get("ok") is False:
            failed_ok += 1
    return {"cases": cases, "tokens": total, "worst_case_tokens": worst_tokens,
            "worst_case_seconds": worst_seconds, "not_ok_cases": failed_ok, "newest_mtime": newest}


def kill_tree(pid: int) -> None:
    try:
        import psutil  # optional
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
        return
    except ImportError:
        pass
    except Exception:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            os.kill(pid, signal.SIGKILL)


def alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--max-tokens", type=int, required=True, help="total tokens across finished cases")
    p.add_argument("--max-case-tokens", type=int, default=400_000)
    p.add_argument("--max-case-seconds", type=float, default=900.0)
    p.add_argument("--max-not-ok", type=int, default=0,
                   help="abort after this many cases with ok=false (0 = off; these are answer failures, not infra errors)")
    p.add_argument("--max-idle-seconds", type=float, default=1200.0,
                   help="abort if no new case file appears for this long (catches a runaway/hung case)")
    p.add_argument("--poll", type=float, default=30.0)
    a = p.parse_args(argv)
    run_dir = Path(a.run_dir)
    started = time.time()
    while alive(a.pid):
        s = scan(run_dir)
        idle = time.time() - max(s["newest_mtime"], started)
        reason = None
        if s["tokens"] > a.max_tokens:
            reason = f"total tokens {s['tokens']:,} > {a.max_tokens:,}"
        elif s["worst_case_tokens"] > a.max_case_tokens:
            reason = f"one case used {s['worst_case_tokens']:,.0f} tokens"
        elif s["worst_case_seconds"] > a.max_case_seconds:
            reason = f"one case took {s['worst_case_seconds']:.0f}s"
        elif a.max_not_ok and s["not_ok_cases"] >= a.max_not_ok:
            reason = f"{s['not_ok_cases']} cases did not finish ok"
        elif idle > a.max_idle_seconds:
            reason = f"no new case file for {idle:.0f}s (a case may be runaway or hung)"
        print(f"[watch] cases={s['cases']} tokens={s['tokens']:,} idle={idle:.0f}s", flush=True)
        if reason:
            print(f"[watch] ABORT: {reason}", flush=True)
            kill_tree(a.pid)
            return 2
        time.sleep(a.poll)
    print("[watch] run process ended", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
