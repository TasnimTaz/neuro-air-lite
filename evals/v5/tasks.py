"""v5 task generator, freezer and variable validator.

Why this exists (see docs/V5_PLAN.md): the paper's headline (+6.4 pp, 450 paired
runs) comes from a 90-task *programmatically verified* benchmark. Each task has

  * a natural-language query,
  * named output variables the agent must leave in the runtime,
  * a ground-truth program, recomputed live and compared per variable with a
    per-variable tolerance (no LLM judge, no prose matching).

The 20-task v2 set cannot show an effect of that size (simulated power ~0% at 20
tasks, ~36% at 90, ~73% at 180). This module generates a large, stratified set
from templates over the schema you already have, freezes the truth values and a
content hash *before any model run*, and scores both arms with one validator.

Workflow
--------
1. ``python -m evals.v5.tasks generate --n 160 --seed 20261122 --out evals/v5/tasks_v5.json``
   (needs DB_URL_ADMIN via core.env.admin_url; recomputes truth; drops tasks whose
   truth is NULL/NaN/errors/trivial; writes sha256).
2. Commit the file. Do not edit it afterwards. ``dev`` tasks (about 20%) are for
   debugging the harness; headline numbers use ``test`` tasks only.
3. ``python evals/run_v5.py ...`` loads it with the hash check.

Truth SQL here is written against domains/hk/schema.sql and follows patterns of
the already-validated v2 cases, but it has not been executed against your
database by the author of this module: step 1 is the validation, and it fails
loudly on any template that errors.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

# --------------------------------------------------------------------------- model

MISSING = object()


@dataclass(frozen=True)
class Output:
    var: str
    desc: str  # e.g. "mean PM2.5 in ug/m3"
    kind: str  # float | int | text
    truth_sql: str
    tol_rel: float = 0.005
    tol_abs: float = 0.0
    expected: Any = None  # filled at freeze time


@dataclass(frozen=True)
class Task:
    id: int
    family: str
    category: str  # retrieval | temporal | statistics | comparative | spatial | multitable
    difficulty: str  # simple | medium | hard
    question: str
    outputs: tuple[Output, ...]
    split: str = "test"  # dev | test

    def var_names(self) -> tuple[str, ...]:
        return tuple(o.var for o in self.outputs)


def output_contract(task: Task) -> str:
    """Text appended to the question. Identical for both arms."""
    lines = [
        "\n\nStore the results in these runtime variables (use exactly these names) "
        "and print each one:"
    ]
    for o in task.outputs:
        kind = {"float": "a number", "int": "an integer", "text": "a short string"}[o.kind]
        lines.append(f"- `{o.var}`: {o.desc} ({kind})")
    lines.append(
        "If a value cannot be determined from the data, leave its variable unset and say why. "
        "Then reply with one short sentence and no code block."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------------ validator


@dataclass
class OutputCheck:
    var: str
    expected: Any
    got: Any
    ok: bool
    reason: str


@dataclass
class ValidatorResult:
    success: bool
    checks: list[OutputCheck] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "checks": [
                {"var": c.var, "expected": c.expected, "got": _plain(c.got), "ok": c.ok, "reason": c.reason}
                for c in self.checks
            ],
        }


def _plain(v: Any) -> Any:
    if v is MISSING:
        return None
    if hasattr(v, "item") and getattr(v, "size", 1) == 1:
        try:
            v = v.item()
        except Exception:  # noqa: BLE001
            pass
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else repr(v)
    return repr(v)[:120]


def _unwrap(v: Any) -> Any:
    """numpy scalar / size-1 Series / 1x1 DataFrame -> python scalar; else unchanged."""
    if hasattr(v, "iloc") and getattr(v, "shape", None) == (1, 1):
        v = v.iloc[0, 0]
    if hasattr(v, "item") and getattr(v, "size", 1) == 1:
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return v
    return v


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def check_output(out: Output, got: Any) -> OutputCheck:
    if got is MISSING:
        return OutputCheck(out.var, out.expected, got, False, "variable not set")
    v = _unwrap(got)
    if out.kind == "text":
        if not isinstance(v, str):
            return OutputCheck(out.var, out.expected, got, False, f"expected text, got {type(v).__name__}")
        ok = _norm_text(v) == _norm_text(str(out.expected))
        return OutputCheck(out.var, out.expected, got, ok, "match" if ok else "text mismatch")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return OutputCheck(out.var, out.expected, got, False, f"expected number, got {type(v).__name__}")
    if not math.isfinite(v):
        return OutputCheck(out.var, out.expected, got, False, "non-finite value")
    exp = float(out.expected)
    if out.kind == "int":
        ok = abs(float(v) - exp) < 1e-9
        return OutputCheck(out.var, out.expected, got, ok, "match" if ok else f"{v} != {exp:g}")
    tol = max(out.tol_abs, out.tol_rel * abs(exp))
    ok = abs(float(v) - exp) <= tol
    return OutputCheck(out.var, out.expected, got, ok, "match" if ok else f"|{v}-{exp:g}|>{tol:g}")


def validate(ns: Mapping[str, Any], task: Task) -> ValidatorResult:
    """A run passes only if every required variable matches (paper Fig. 5)."""
    checks = [check_output(o, ns.get(o.var, MISSING)) for o in task.outputs]
    return ValidatorResult(all(c.ok for c in checks), checks)


# --------------------------------------------------------------------- generation

POLLUTANTS = {
    "pm2_5": ("PM2.5", "ug/m3", 25.0),
    "pm10": ("PM10", "ug/m3", 50.0),
    "no2": ("NO2", "ug/m3", 40.0),
    "o3": ("ozone", "ug/m3", None),
    "so2": ("SO2", "ug/m3", None),
}
EXCEED_POLS = ("pm2_5", "pm10", "no2")
CORR_PAIRS = (("pm2_5", "pm10"), ("pm2_5", "no2"), ("no2", "o3"), ("pm10", "no2"))
YEAR = 2025
AQ = "hourly_station_air_quality a JOIN air_quality_station s ON s.id = a.station_id"
PAIRED_WX = (
    "hourly_station_air_quality a "
    "JOIN air_quality_station s ON s.id = a.station_id "
    "JOIN weather_station ws ON ws.station_code = 'wx_' || s.station_code "
    "JOIN hourly_station_weather w ON w.station_id = ws.id AND w.datetime = a.datetime"
)


def _q(s: str) -> str:
    return s.replace("'", "''")


def _month(m: int) -> tuple[str, str, str]:
    start = f"{YEAR}-{m:02d}-01"
    end = f"{YEAR + 1}-01-01" if m == 12 else f"{YEAR}-{m + 1:02d}-01"
    return start, end, f"{calendar.month_name[m]} {YEAR}"


def _win(m: int, alias: str = "a") -> str:
    s, e, _ = _month(m)
    return f"{alias}.datetime >= '{s}' AND {alias}.datetime < '{e}'"


_YEAR_WIN = f"a.datetime >= '{YEAR}-01-01' AND a.datetime < '{YEAR + 1}-01-01'"
_NUM = {"tol_rel": 0.005, "tol_abs": 0.02}


def _o(var, desc, kind, sql, **kw) -> Output:
    if kind == "float":
        kw = {**_NUM, **kw}
    return Output(var=var, desc=desc, kind=kind, truth_sql=sql, **kw)


Entities = Mapping[str, Any]  # {"stations": [{"name","type","area"}], "paired": [names]}
Family = Callable[[random.Random, Entities], dict]


def _station(rng: random.Random, ent: Entities, *, paired: bool = False, kind: str | None = None) -> str:
    pool = ent["paired"] if paired else [s["name"] for s in ent["stations"] if kind in (None, s["type"])]
    return rng.choice(sorted(pool))


def f_retrieval_mean(rng, ent):
    st, pol, m = _station(rng, ent), rng.choice(list(POLLUTANTS)), rng.randint(1, 12)
    label, unit, _ = POLLUTANTS[pol]
    return dict(
        category="retrieval", difficulty="medium",
        question=f"What was the average {label} at the {st} air quality station in {_month(m)[2]}, "
                 f"and for how many hours that month did it have a valid {label} reading?",
        outputs=(
            _o(f"mean_{pol}", f"mean {label} in {unit}", "float",
               f"SELECT round(avg(a.{pol})::numeric, 2) FROM {AQ} WHERE s.name_en = '{_q(st)}' AND {_win(m)}"),
            _o("valid_hours", f"number of hours with a non-null {label} reading", "int",
               f"SELECT count(a.{pol}) FROM {AQ} WHERE s.name_en = '{_q(st)}' AND {_win(m)}"),
        ),
    )


def f_retrieval_altitude(rng, ent):
    st = _station(rng, ent)
    return dict(
        category="retrieval", difficulty="simple",
        question=f"How high above sea level is the {st} air quality station?",
        outputs=(_o("altitude_m", "station altitude in metres", "float",
                    f"SELECT round(altitude::numeric, 1) FROM air_quality_station WHERE name_en = '{_q(st)}'",
                    tol_abs=0.1),),
    )


def f_temporal_month_diff(rng, ent):
    st, pol = _station(rng, ent), rng.choice(list(POLLUTANTS))
    m1, m2 = rng.sample(range(1, 13), 2)
    label, unit, _ = POLLUTANTS[pol]
    return dict(
        category="temporal", difficulty="medium",
        question=f"At the {st} air quality station, how much higher was the average {label} in "
                 f"{_month(m1)[2]} than in {_month(m2)[2]}? (A negative number if it was lower.)",
        outputs=(_o(f"diff_{pol}", f"mean {label} in {_month(m1)[2]} minus mean {label} in {_month(m2)[2]}, {unit}", "float",
                    f"SELECT round((avg(a.{pol}) FILTER (WHERE {_win(m1)}) - avg(a.{pol}) FILTER (WHERE {_win(m2)}))::numeric, 2) "
                    f"FROM {AQ} WHERE s.name_en = '{_q(st)}' AND (({_win(m1)}) OR ({_win(m2)}))"),),
    )


def f_temporal_weekend(rng, ent):
    st, pol = _station(rng, ent), rng.choice(["pm2_5", "pm10", "no2", "o3"])
    label, unit, _ = POLLUTANTS[pol]
    return dict(
        category="temporal", difficulty="medium",
        question=f"At the {st} air quality station, how much higher is the average {label} on weekends "
                 f"than on weekdays across {YEAR}?",
        outputs=(_o(f"weekend_minus_weekday_{pol}", f"weekend mean minus weekday mean of {label}, {unit}", "float",
                    f"SELECT round((avg(a.{pol}) FILTER (WHERE extract(isodow FROM a.datetime) >= 6) "
                    f"- avg(a.{pol}) FILTER (WHERE extract(isodow FROM a.datetime) < 6))::numeric, 2) "
                    f"FROM {AQ} WHERE s.name_en = '{_q(st)}' AND {_YEAR_WIN}", tol_abs=0.05),),
    )


def f_temporal_peak_hour(rng, ent):
    st, pol, m = _station(rng, ent), rng.choice(["pm2_5", "no2", "o3"]), rng.randint(1, 12)
    label, unit, _ = POLLUTANTS[pol]
    inner = (f"SELECT extract(hour FROM a.datetime)::int AS h, avg(a.{pol}) AS m FROM {AQ} "
             f"WHERE s.name_en = '{_q(st)}' AND {_win(m)} GROUP BY 1 HAVING avg(a.{pol}) IS NOT NULL ORDER BY m DESC, h LIMIT 1")
    return dict(
        category="temporal", difficulty="hard",
        question=f"In {_month(m)[2]}, at which hour of the day (0-23, local time) was the average {label} "
                 f"highest at the {st} air quality station, and what was that average?",
        outputs=(_o("peak_hour", "hour of day 0-23 with the highest mean", "int", f"SELECT h FROM ({inner}) t"),
                 _o("peak_hour_mean", f"mean {label} at that hour, {unit}", "float",
                    f"SELECT round(m::numeric, 2) FROM ({inner}) t")),
    )


def f_stats_p95(rng, ent):
    st, pol = _station(rng, ent), rng.choice(["pm2_5", "pm10", "no2", "o3"])
    label, unit, _ = POLLUTANTS[pol]
    return dict(
        category="statistics", difficulty="medium",
        question=f"What was the 95th percentile of hourly {label} at the {st} air quality station in {YEAR}?",
        outputs=(_o(f"p95_{pol}", f"95th percentile of hourly {label}, {unit}", "float",
                    f"SELECT round((percentile_cont(0.95) WITHIN GROUP (ORDER BY a.{pol}))::numeric, 2) "
                    f"FROM {AQ} WHERE s.name_en = '{_q(st)}' AND {_YEAR_WIN}"),),
    )


def f_stats_exceed(rng, ent):
    st, pol = _station(rng, ent), rng.choice(EXCEED_POLS)
    label, unit, thr = POLLUTANTS[pol]
    daily = (f"SELECT avg(a.{pol}) AS m FROM {AQ} WHERE s.name_en = '{_q(st)}' AND {_YEAR_WIN} "
             f"GROUP BY date_trunc('day', a.datetime)")
    return dict(
        category="statistics", difficulty="hard",
        question=f"At the {st} air quality station in {YEAR}, on how many days did the daily average "
                 f"{label} exceed {thr:g} {unit}, and what was the highest daily average?",
        outputs=(_o("days_exceeding", f"number of calendar days with daily mean {label} above {thr:g}", "int",
                    f"SELECT count(*) FROM ({daily}) t WHERE m > {thr}"),
                 _o("max_daily_mean", f"highest daily mean {label}, {unit}", "float",
                    f"SELECT round(max(m)::numeric, 2) FROM ({daily}) t")),
    )


def f_stats_corr(rng, ent):
    st = _station(rng, ent)
    p1, p2 = rng.choice(CORR_PAIRS)
    l1, l2 = POLLUTANTS[p1][0], POLLUTANTS[p2][0]
    return dict(
        category="statistics", difficulty="medium",
        question=f"What is the Pearson correlation between hourly {l1} and {l2} at the {st} air quality station in {YEAR}?",
        outputs=(_o(f"corr_{p1}_{p2}", f"Pearson r of hourly {l1} vs {l2}", "float",
                    f"SELECT round(corr(a.{p1}, a.{p2})::numeric, 3) FROM {AQ} WHERE s.name_en = '{_q(st)}' AND {_YEAR_WIN}",
                    tol_abs=0.01),),
    )


def f_comp_road_general(rng, ent):
    pol, m = rng.choice(["pm2_5", "pm10", "no2"]), rng.randint(1, 12)
    label, unit, _ = POLLUTANTS[pol]

    def grp(t):
        return (f"SELECT round(avg(a.{pol})::numeric, 2) FROM {AQ} WHERE s.station_type = '{t}' AND {_win(m)}")

    return dict(
        category="comparative", difficulty="hard",
        question=f"Pooling all hourly readings in {_month(m)[2]}, what was the mean {label} at roadside stations, "
                 f"at general stations, and how much higher (or lower) was roadside than general?",
        outputs=(_o("roadside_mean", f"mean {label} over all roadside-station hours, {unit}", "float", grp("ROADSIDE")),
                 _o("general_mean", f"mean {label} over all general-station hours, {unit}", "float", grp("GENERAL")),
                 _o("roadside_minus_general", "roadside_mean minus general_mean", "float",
                    f"SELECT round(((SELECT avg(a.{pol}) FROM {AQ} WHERE s.station_type = 'ROADSIDE' AND {_win(m)}) - "
                    f"(SELECT avg(a.{pol}) FROM {AQ} WHERE s.station_type = 'GENERAL' AND {_win(m)}))::numeric, 2)")),
    )


def f_comp_top_station(rng, ent):
    pol, m = rng.choice(["pm2_5", "pm10", "no2", "o3"]), rng.randint(1, 12)
    label, unit, _ = POLLUTANTS[pol]
    inner = (f"SELECT s.name_en AS n, avg(a.{pol}) AS m FROM {AQ} WHERE {_win(m)} GROUP BY s.name_en "
             f"HAVING avg(a.{pol}) IS NOT NULL ORDER BY m DESC, n LIMIT 1")
    return dict(
        category="comparative", difficulty="medium",
        question=f"Which air quality station had the highest average {label} in {_month(m)[2]}, and what was that average?",
        outputs=(_o("top_station", "station name exactly as in the database", "text", f"SELECT n FROM ({inner}) t"),
                 _o("top_station_mean", f"that station's mean {label}, {unit}", "float",
                    f"SELECT round(m::numeric, 2) FROM ({inner}) t")),
    )


def f_comp_rank(rng, ent):
    st, pol = _station(rng, ent), rng.choice(["pm2_5", "pm10", "no2"])
    label, _, _ = POLLUTANTS[pol]
    inner = (f"SELECT s.name_en AS n, rank() OVER (ORDER BY avg(a.{pol}) DESC) AS r FROM {AQ} "
             f"WHERE {_YEAR_WIN} GROUP BY s.name_en HAVING avg(a.{pol}) IS NOT NULL")
    return dict(
        category="comparative", difficulty="medium",
        question=f"Ranking all air quality stations by their {YEAR} average {label} (1 = highest), "
                 f"what rank does the {st} station have?",
        outputs=(_o("station_rank", "rank, 1 = highest annual mean", "int",
                    f"SELECT r FROM ({inner}) t WHERE n = '{_q(st)}'"),),
    )


def f_spatial_nearest(rng, ent):
    st = _station(rng, ent)
    base = (f"FROM air_quality_station s1 JOIN air_quality_station s2 ON s2.id <> s1.id "
            f"WHERE s1.name_en = '{_q(st)}' ORDER BY ST_Distance(s1.coordinate, s2.coordinate), s2.name_en LIMIT 1")
    return dict(
        category="spatial", difficulty="medium",
        question=f"Which other air quality station is closest to the {st} station, and how far away is it?",
        outputs=(_o("nearest_station", "name of the closest other station", "text", f"SELECT s2.name_en {base}"),
                 _o("nearest_distance_m", "straight-line geodesic distance in metres", "float",
                    f"SELECT round(ST_Distance(s1.coordinate, s2.coordinate)::numeric, 0) {base}",
                    tol_rel=0.01, tol_abs=5.0)),
    )


def f_spatial_within_stations(rng, ent):
    st, r = _station(rng, ent), rng.choice([3, 5, 8, 10])
    return dict(
        category="spatial", difficulty="simple",
        question=f"How many other air quality stations are within {r} km of the {st} station?",
        outputs=(_o("stations_within", f"count of other stations within {r} km", "int",
                    f"SELECT count(*) FROM air_quality_station s1 JOIN air_quality_station s2 "
                    f"ON s2.id <> s1.id AND ST_DWithin(s1.coordinate, s2.coordinate, {r * 1000}) "
                    f"WHERE s1.name_en = '{_q(st)}'"),),
    )


def f_spatial_sensitive(rng, ent):
    st, r = _station(rng, ent), rng.choice([1000, 1500, 2000])

    def cnt(cat):
        return (f"SELECT count(*) FROM sensitive_area sa, air_quality_station s WHERE s.name_en = '{_q(st)}' "
                f"AND sa.category = '{cat}' AND ST_DWithin(sa.geometry::geography, s.coordinate, {r})")

    return dict(
        category="spatial", difficulty="hard",
        question=f"How many schools and how many hospitals are within {r / 1000:g} km of the {st} air quality station?",
        outputs=(_o("school_count", f"number of educational institutions within {r} m", "int", cnt("EDUCATIONAL_INSTITUTION")),
                 _o("hospital_count", f"number of health-care institutions within {r} m", "int", cnt("HEALTH_CARE_INSTITUTION"))),
    )


def f_multi_wind(rng, ent):
    st, m = _station(rng, ent, paired=True), rng.randint(1, 12)
    base = (f"FROM hourly_station_weather w JOIN weather_station ws ON ws.id = w.station_id "
            f"JOIN air_quality_station s ON ws.station_code = 'wx_' || s.station_code "
            f"WHERE s.name_en = '{_q(st)}' AND {_win(m, 'w')}")
    return dict(
        category="multitable", difficulty="medium",
        question=f"Using the weather station that belongs with the {st} air quality station, what was the "
                 f"average wind speed and the average temperature in {_month(m)[2]}?",
        outputs=(_o("mean_wind_speed", "mean wind speed, m/s", "float", f"SELECT round(avg(w.wind_speed)::numeric, 2) {base}"),
                 _o("mean_temperature", "mean temperature, degrees C", "float", f"SELECT round(avg(w.temperature)::numeric, 2) {base}")),
    )


def f_multi_calm(rng, ent):
    st, m, thr = _station(rng, ent, paired=True), rng.randint(1, 12), rng.choice([1.5, 2.0, 3.0])
    base = f"FROM {PAIRED_WX} WHERE s.name_en = '{_q(st)}' AND {_win(m)} AND w.wind_speed < {thr} AND a.pm2_5 IS NOT NULL"
    return dict(
        category="multitable", difficulty="hard",
        question=f"At the {st} air quality station in {_month(m)[2]}, what was the average PM2.5 during hours when the "
                 f"paired weather station recorded wind speed below {thr:g} m/s, and how many such hours were there?",
        outputs=(_o("mean_pm25_calm", "mean PM2.5 over those hours, ug/m3", "float", f"SELECT round(avg(a.pm2_5)::numeric, 2) {base}"),
                 _o("calm_hours", "number of hours with a PM2.5 reading and wind below the threshold", "int", f"SELECT count(*) {base}")),
    )


# family -> weight. Mirrors the paper's PV mix (spatial 30%, retrieval 22%, comparative 20%,
# statistical 18%, temporal 10%) plus a multi-table weather group where the repo's own
# failure logs show most of the multi-agent losses and wins.
FAMILIES: tuple[tuple[str, Family, float], ...] = (
    ("retrieval_mean", f_retrieval_mean, 0.12),
    ("retrieval_altitude", f_retrieval_altitude, 0.06),
    ("temporal_month_diff", f_temporal_month_diff, 0.04),
    ("temporal_weekend", f_temporal_weekend, 0.03),
    ("temporal_peak_hour", f_temporal_peak_hour, 0.03),
    ("stats_p95", f_stats_p95, 0.05),
    ("stats_exceed", f_stats_exceed, 0.08),
    ("stats_corr", f_stats_corr, 0.05),
    ("comp_road_general", f_comp_road_general, 0.06),
    ("comp_top_station", f_comp_top_station, 0.05),
    ("comp_rank", f_comp_rank, 0.05),
    ("spatial_nearest", f_spatial_nearest, 0.08),
    ("spatial_within_stations", f_spatial_within_stations, 0.05),
    ("spatial_sensitive", f_spatial_sensitive, 0.12),
    ("multi_wind", f_multi_wind, 0.06),
    ("multi_calm", f_multi_calm, 0.07),
)


def _split(question: str) -> str:
    return "dev" if int(hashlib.sha1(question.encode()).hexdigest()[:8], 16) % 5 == 0 else "test"


def build_tasks(entities: Entities, n: int, seed: int) -> list[Task]:
    """Deterministic, de-duplicated, family-stratified candidate tasks (truth not yet computed)."""
    rng = random.Random(seed)
    total_w = sum(w for _, _, w in FAMILIES)
    quota = {name: max(1, round(n * w / total_w)) for name, _, w in FAMILIES}
    tasks: list[Task] = []
    seen: set[str] = set()
    for name, fn, _ in FAMILIES:
        made, attempts = 0, 0
        while made < quota[name] and attempts < quota[name] * 60:
            attempts += 1
            spec = fn(rng, entities)
            if spec["question"] in seen:
                continue
            seen.add(spec["question"])
            tasks.append(Task(id=0, family=name, split=_split(spec["question"]), **spec))
            made += 1
    rng.shuffle(tasks)
    return [Task(**{**asdict_shallow(t), "id": i + 1}) for i, t in enumerate(tasks)]


def asdict_shallow(t: Task) -> dict:
    return {f: getattr(t, f) for f in t.__dataclass_fields__}


# --------------------------------------------------------------------------- freeze

ScalarRunner = Callable[[str], Any]


def freeze(tasks: list[Task], run_scalar: ScalarRunner) -> tuple[list[Task], list[dict]]:
    """Compute and pin truth. Returns (kept tasks, dropped log). Never keeps a NULL/NaN/error truth."""
    kept: list[Task] = []
    dropped: list[dict] = []
    for t in tasks:
        outs: list[Output] = []
        reason = ""
        for o in t.outputs:
            try:
                val = run_scalar(o.truth_sql)
            except Exception as exc:  # noqa: BLE001
                reason = f"{o.var}: SQL error {type(exc).__name__}: {str(exc)[:160]}"
                break
            val = _unwrap(val)
            if val is None:
                reason = f"{o.var}: truth is NULL"
                break
            if o.kind in ("int", "float"):
                try:
                    val = float(val)  # Decimal from round(numeric) -> float
                except (TypeError, ValueError):
                    reason = f"{o.var}: truth is not numeric ({val!r})"
                    break
                if not math.isfinite(val):
                    reason = f"{o.var}: truth is NaN/inf"
                    break
                if o.kind == "int":
                    val = int(val)
            else:
                val = str(val)
            outs.append(Output(**{**asdict(o), "expected": val}))
        if not reason and all(o.kind == "int" and o.expected == 0 for o in outs):
            reason = "all integer truths are 0 (guessable)"
        if reason:
            dropped.append({"family": t.family, "question": t.question, "reason": reason})
        else:
            kept.append(Task(**{**asdict_shallow(t), "outputs": tuple(outs)}))
    kept = [Task(**{**asdict_shallow(t), "id": i + 1}) for i, t in enumerate(kept)]
    return kept, dropped


def tasks_digest(tasks: list[Task]) -> str:
    payload = json.dumps([_task_json(t) for t in tasks], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _task_json(t: Task) -> dict:
    d = asdict_shallow(t)
    d["outputs"] = [asdict(o) for o in t.outputs]
    return d


def save_tasks(path: Path, tasks: list[Task], *, seed: int, dropped: list[dict]) -> str:
    digest = tasks_digest(tasks)
    path.write_text(
        json.dumps(
            {"version": "v5", "seed": seed, "n": len(tasks), "sha256": digest,
             "by_category": _count(tasks, "category"), "by_split": _count(tasks, "split"),
             "dropped": dropped, "tasks": [_task_json(t) for t in tasks]},
            indent=1, default=str),
        encoding="utf-8",
    )
    return digest


def load_tasks(path: Path, *, verify: bool = True) -> list[Task]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    tasks = [
        Task(**{**{k: v for k, v in t.items() if k != "outputs"},
                "outputs": tuple(Output(**o) for o in t["outputs"])})
        for t in raw["tasks"]
    ]
    if verify and tasks_digest(tasks) != raw["sha256"]:
        raise ValueError(f"{path} was modified after it was frozen (sha256 mismatch)")
    return tasks


def _count(tasks: list[Task], attr: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        out[getattr(t, attr)] = out.get(getattr(t, attr), 0) + 1
    return dict(sorted(out.items()))


# ------------------------------------------------------------------------------ CLI


def fetch_entities(engine) -> dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as c:
        stations = [
            {"name": r[0], "type": str(r[1]), "area": str(r[2])}
            for r in c.execute(text("SELECT name_en, station_type::text, area_type::text FROM air_quality_station ORDER BY name_en"))
        ]
        paired = [
            r[0]
            for r in c.execute(text(
                "SELECT s.name_en FROM air_quality_station s "
                "JOIN weather_station ws ON ws.station_code = 'wx_' || s.station_code ORDER BY s.name_en"))
        ]
    return {"stations": stations, "paired": paired}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--n", type=int, default=160, help="candidate tasks before dropping bad truth")
    g.add_argument("--seed", type=int, default=20261122)
    g.add_argument("--out", type=Path, default=Path("evals/v5/tasks_v5.json"))
    sub.add_parser("check").add_argument("path", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "check":
        tasks = load_tasks(args.path)
        print(f"ok: {len(tasks)} tasks, sha256 verified; categories={_count(tasks, 'category')}")
        return 0

    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from sqlalchemy import text

    from core.env import admin_url
    from domains.hk.db import make_admin_engine

    engine = make_admin_engine(admin_url())
    ent = fetch_entities(engine)
    candidates = build_tasks(ent, args.n, args.seed)

    def run_scalar(sql: str):
        with engine.connect() as c:
            return c.execute(text(sql)).scalar()

    kept, dropped = freeze(candidates, run_scalar)
    digest = save_tasks(args.out, kept, seed=args.seed, dropped=dropped)
    print(f"candidates={len(candidates)} kept={len(kept)} dropped={len(dropped)} sha256={digest}")
    print("by_category:", _count(kept, "category"), "by_split:", _count(kept, "split"))
    for d in dropped[:12]:
        print(" dropped:", d["family"], "|", d["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
