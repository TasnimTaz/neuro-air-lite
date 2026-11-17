"""A tiny foreground daemon for the deterministic Tai Po reference.

The daemon is a drift monitor, not a web service:

* every check creates a read-only engine, runs the fixed reference queries and
  disposes the engine;
* a database error or a reference mismatch is reported as ``degraded`` and the
  loop continues, so a temporary outage does not create a crash loop;
* it prints one JSON object per line and writes no files or database rows.

Examples::

    uv run python -m analysis.daemon --once
    uv run python -m analysis.daemon --interval 300
    uv run python -m analysis.daemon --interval 30 --max-checks 10

``--once`` exits non-zero when the snapshot is degraded. The long-running form
only exits on Ctrl-C or after ``--max-checks`` checks.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from analysis.tai_po_reference import compute_reference
from core.env import load_agent_env
from domains.hk.db import make_ro_engine


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReferenceDaemon:
    """Run the Tai Po reference on an interval and emit JSON lines."""

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] = make_ro_engine,
        fetch: Callable[[Any], dict[str, Any]] = compute_reference,
        interval_seconds: float = 60.0,
        max_checks: int = 0,
        sleep: Callable[[float], None] = time.sleep,
        emit: Callable[[str], None] = print,
    ) -> None:
        self.engine_factory = engine_factory
        self.fetch = fetch
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.max_checks = max(0, int(max_checks))
        self.sleep = sleep
        self.emit = emit
        self._checks = 0

    def check_once(self) -> dict[str, Any]:
        """Run one check; never raise for a DB/fetch failure."""
        started = time.perf_counter()
        self._checks += 1
        engine = None
        try:
            engine = self.engine_factory()
            report = self.fetch(engine)
            ok = bool(report.get("ok"))
            return {
                "checked_at": _utc_now(),
                "check": self._checks,
                "ok": ok,
                "degraded": not ok,
                "seconds": round(time.perf_counter() - started, 3),
                "report": report,
            }
        except Exception as error:  # noqa: BLE001 - the daemon must survive
            return {
                "checked_at": _utc_now(),
                "check": self._checks,
                "ok": False,
                "degraded": True,
                "seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(error).__name__}: {error}",
            }
        finally:
            dispose = getattr(engine, "dispose", None)
            if callable(dispose):
                dispose()

    def run(self) -> dict[str, Any]:
        """Run until ``max_checks`` is reached (0 means forever)."""
        last: dict[str, Any] = {}
        while self.max_checks == 0 or self._checks < self.max_checks:
            last = self.check_once()
            self.emit(json.dumps(last, ensure_ascii=False, default=str))
            if self.max_checks and self._checks >= self.max_checks:
                break
            self.sleep(self.interval_seconds)
        return last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one check, print it, and exit non-zero if degraded",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="seconds between checks in long-running mode (default: 60)",
    )
    parser.add_argument(
        "--max-checks",
        type=int,
        default=0,
        help="stop after N checks; 0 means run until Ctrl-C (default: 0)",
    )
    args = parser.parse_args(argv)

    load_agent_env(override=True, scrub=True)
    daemon = ReferenceDaemon(
        interval_seconds=args.interval,
        max_checks=args.max_checks,
    )
    if args.once:
        snapshot = daemon.check_once()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        return 0 if snapshot["ok"] else 1

    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
