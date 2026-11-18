"""A tiny foreground daemon for the deterministic Tai Po reference.

The daemon keeps the Day 8 drift monitor and adds the paper's AQHI alert path:

* every check creates a read-only engine, runs the fixed reference queries or
  the latest-AQHI query, and disposes the engine;
* a database error or a reference mismatch is reported as ``degraded`` and the
  loop continues, so a temporary outage does not create a crash loop;
* it prints one JSON object per line and writes no files or database rows;
* ``--alerts`` triggers when a station's latest hourly AQHI reaches
  ``AQHI_ALERT`` (default 7), with a per-station cooldown (default 3600 s);
* ``REPLAY_NOW`` is authoritative for the alert path, so a replay never
  silently uses wall-clock time.

Examples::

    uv run python -m analysis.daemon --once
    uv run python -m analysis.daemon --interval 300
    uv run python -m analysis.daemon --interval 30 --max-checks 10
    uv run python -m analysis.daemon --alerts --once
    REPLAY_NOW='2025-11-26 15:30' uv run python -m analysis.daemon --alerts --once

``--once`` exits non-zero when the snapshot is degraded. The long-running form
only exits on Ctrl-C or after ``--max-checks`` checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, MutableMapping

from sqlalchemy import text

from analysis.tai_po_reference import compute_reference
from core.env import load_agent_env
from domains.hk.db import make_ro_engine

LOGGER = logging.getLogger(__name__)

AQHI_ALERT_DEFAULT = 7
AQHI_ALERT_COOLDOWN_SECONDS = 60 * 60
DEFAULT_JOB_TEMPLATE = (
    "Assess the air quality situation near {station} and nearby sensitive receptors."
)
_ALERT_LAST_FIRED: dict[str, datetime] = {}

# Latest hourly AQHI per station at or before ``now``. The query is deliberately
# portable (no DISTINCT ON) so the synthetic SQLite tests exercise the same SQL
# as the live PostgreSQL run.
LATEST_AQHI_SQL = text(
    """
    SELECT
      s.station_code,
      s.name_en,
      a.datetime,
      a.aqhi,
      CAST(a.aqhi_level AS TEXT) AS aqhi_level
    FROM hourly_station_air_quality a
    JOIN air_quality_station s ON s.id = a.station_id
    JOIN (
      SELECT station_id, max(datetime) AS latest
      FROM hourly_station_air_quality
      WHERE datetime <= :now
      GROUP BY station_id
    ) latest
      ON latest.station_id = a.station_id
     AND latest.latest = a.datetime
    WHERE a.aqhi IS NOT NULL
    ORDER BY s.station_code
    """
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("ignoring non-integer %s=%r; using %d", name, raw, default)
        return default


def _dt_param(value: datetime) -> str:
    """Render a naive HKT datetime in a form both PostgreSQL and SQLite accept."""
    return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def resolve_now(now: datetime | str | None = None) -> datetime:
    """Resolve the clock used by the alert path.

    ``REPLAY_NOW`` is authoritative when it is set, so a replay never silently
    falls back to wall-clock time. An explicit argument wins over the
    environment, which keeps tests deterministic.
    """
    if isinstance(now, datetime):
        return now.replace(tzinfo=None)
    if isinstance(now, str):
        return datetime.fromisoformat(now).replace(tzinfo=None)
    replay = os.environ.get("REPLAY_NOW")
    if replay:
        return datetime.fromisoformat(replay).replace(tzinfo=None)
    return datetime.now()


def _cooldown_timedelta(cooldown: timedelta | int | float | None) -> timedelta:
    if cooldown is None:
        seconds = _env_int(
            "AQHI_ALERT_COOLDOWN_SECONDS", AQHI_ALERT_COOLDOWN_SECONDS
        )
        return timedelta(seconds=max(0, seconds))
    if isinstance(cooldown, timedelta):
        return cooldown if cooldown >= timedelta(0) else timedelta(0)
    return timedelta(seconds=max(0.0, float(cooldown)))


def check_alerts(
    conn: Any,
    now: datetime | str | None = None,
    threshold: int | None = None,
    cooldown: timedelta | int | float | None = None,
    *,
    state: MutableMapping[str, datetime] | None = None,
    run_job: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Return alerts for stations whose latest hourly AQHI reaches threshold.

    The alert is deliberately a *trigger*, not an assessment: the default job
    text asks the existing agent to assess the situation. ``state`` holds the
    last-fired time per station; a station inside its cooldown is skipped.
    ``run_job`` is pluggable so the daemon can hand the query to the real agent
    without making this module import the agent runtime.
    """
    now_dt = resolve_now(now)
    limit = _env_int("AQHI_ALERT", AQHI_ALERT_DEFAULT) if threshold is None else int(threshold)
    cooldown_dt = _cooldown_timedelta(cooldown)
    last_fired = state if state is not None else _ALERT_LAST_FIRED
    dispatch = run_job or (
        lambda query: LOGGER.info("alert job: %s", query)
    )

    rows = conn.execute(LATEST_AQHI_SQL, {"now": _dt_param(now_dt)}).mappings().all()
    triggers: list[dict[str, Any]] = []
    for row in rows:
        aqhi = row.get("aqhi")
        if aqhi is None or int(aqhi) < limit:
            continue
        station_code = str(row["station_code"])
        last = last_fired.get(station_code)
        if last is not None and now_dt - last < cooldown_dt:
            continue
        station_name = row.get("name_en") or station_code
        query = DEFAULT_JOB_TEMPLATE.format(station=station_name)
        trigger = {
            "station_code": station_code,
            "station": station_name,
            "observed_at": str(row["datetime"]),
            "aqhi": int(aqhi),
            "aqhi_level": row.get("aqhi_level"),
            "threshold": limit,
            "query": query,
            "triggered_at": now_dt.isoformat(timespec="seconds"),
        }
        last_fired[station_code] = now_dt
        triggers.append(trigger)
        dispatch(query)
    return triggers


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


def alert_check_once(
    *,
    now: datetime | str | None = None,
    threshold: int | None = None,
    cooldown: timedelta | int | float | None = None,
    engine_factory: Callable[[], Any] = make_ro_engine,
) -> dict[str, Any]:
    """Open a read-only engine, evaluate alerts, and dispose the engine."""
    engine = None
    now_dt = resolve_now(now)
    effective_threshold = (
        int(threshold)
        if threshold is not None
        else _env_int("AQHI_ALERT", AQHI_ALERT_DEFAULT)
    )
    try:
        engine = engine_factory()
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            triggers = check_alerts(
                conn,
                now_dt,
                threshold=effective_threshold,
                cooldown=cooldown,
            )
            conn.rollback()
        return {
            "checked_at": _utc_now(),
            "now": now_dt.isoformat(timespec="seconds"),
            "replay": bool(os.environ.get("REPLAY_NOW")) or now is not None,
            "threshold": effective_threshold,
            "cooldown_seconds": _cooldown_timedelta(cooldown).total_seconds(),
            "trigger_count": len(triggers),
            "triggers": triggers,
        }
    except Exception as error:  # noqa: BLE001 - keep the daemon alive
        return {
            "checked_at": _utc_now(),
            "now": now_dt.isoformat(timespec="seconds"),
            "replay": bool(os.environ.get("REPLAY_NOW")) or now is not None,
            "threshold": effective_threshold,
            "trigger_count": 0,
            "triggers": [],
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        dispose = getattr(engine, "dispose", None)
        if callable(dispose):
            dispose()


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
    parser.add_argument(
        "--alerts",
        action="store_true",
        help=(
            "run the AQHI alert path instead of the reference drift check; "
            "REPLAY_NOW is honoured and never falls back to wall clock"
        ),
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="AQHI alert threshold; default from AQHI_ALERT or 7",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=None,
        help=(
            "seconds before the same station can alert again; default from "
            "AQHI_ALERT_COOLDOWN_SECONDS or 3600"
        ),
    )
    parser.add_argument(
        "--now",
        default=None,
        help="explicit naive HKT replay time, e.g. '2025-11-26 15:30'",
    )
    args = parser.parse_args(argv)

    load_agent_env(override=True, scrub=True)
    if args.alerts:
        if args.once or args.max_checks == 1:
            snapshot = alert_check_once(
                now=args.now,
                threshold=args.threshold,
                cooldown=args.cooldown,
            )
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
            return 0 if "error" not in snapshot else 1
        checks = 0
        last: dict[str, Any] = {}
        while args.max_checks == 0 or checks < args.max_checks:
            checks += 1
            last = alert_check_once(
                now=args.now,
                threshold=args.threshold,
                cooldown=args.cooldown,
            )
            print(json.dumps(last, ensure_ascii=False, default=str))
            if "error" in last:
                return 1
            if args.max_checks and checks >= args.max_checks:
                break
            time.sleep(max(0.0, args.interval))
        return 0 if last and "error" not in last else 1

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
