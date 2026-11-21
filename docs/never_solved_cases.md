# Never-solved cases audit: v2 / v3 / v4, both arms

Read-only audit. No eval/ question, truth value, tolerance, scorer, prompt, role,
core file or run artifact was changed. Only offline JSON parsing was used; no
LLM or evaluation command was run.

## Scope of the six run slots

| Slot | Directory used | Note |
|---|---|---|
| single v2 | `runs/baseline/day4_v2_budget20` | `runs/baseline/day5b_v2_budget20_3x` does not exist in the workspace directory listing, so the stated fallback was used. |
| multi v2 | `runs/multi/day5b_v2_budget20_3x` | Official non-pre-scorer-fix run. The `..._pre_scorer_fix` directory has no `run.json` and only 5 case files (`repeat-01-case-01..05.json`), so it was not used as a sixth slot. |
| single v3 | `runs/baseline/day5b_v3_budget20_3x` | |
| multi v3 | `runs/multi/day5b_v3_budget20_3x` | |
| single v4 | `runs/baseline/day9_v4_budget20_3x` | |
| multi v4 | `runs/multi/day9_v4_budget20_3x` | |

All six `run.json` files report `arm`, `repeats: 3` and 20 entries in
`summary.per_case`. Counts below are `summary.per_case[].pass` with `n = 3`.
Cross-check: for the five runs that also have `cases/*.json`, the `summary`
pass count matched the count of `pass == true` in the case files for all 20
cases (0 mismatches), and across all 300 inspected case files `pass` equalled
`pass_v2` (0 mismatches). The single-v2 fallback has no `cases/` directory, so
only its `summary.per_case` was available.

## Pass counts per case

Each cell is `passes / 3` for that run. `Total` is out of 18.

| Case | Question (as stored in the run artifacts) | sv2 (day4 fallback) | mv2 | sv3 | mv3 | sv4 | mv4 | Total /18 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | How high above sea level is the Tseung Kwan O air quality station? | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 18 |
| 2 | How many general air quality stations are in new-town areas? | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 18 |
| 3 | What was the average CO concentration at the Sha Tin air quality station in 2025? | 3/3 | 3/3 | 3/3 | 2/3 | 3/3 | 1/3 | 15 |
| 4 | What was the average PM2.5 at the Kennedy Town air quality station in 2025? | 3/3 | 2/3 | 3/3 | 2/3 | 3/3 | 1/3 | 14 |
| 5 | At Shatin, how much warmer was August than January 2025 on average? | 1/3 | 0/3 | 1/3 | 2/3 | 2/3 | 3/3 | 9 |
| 6 | At Tung Chung, how much higher is the average PM2.5 on weekends than on weekdays in 2025? | 3/3 | 3/3 | 3/3 | 2/3 | 2/3 | 2/3 | 15 |
| 7 | What was Yuen Long's highest monthly average PM2.5 in 2025? | 3/3 | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 17 |
| 8 | What was the average PM2.5 at Tai Po in January 2026? | 3/3 | 2/3 | 3/3 | 2/3 | 3/3 | 0/3 | 13 |
| 9 | How many schools are within 1 km of the Tsuen Wan air quality station? | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 18 |
| 10 | How many schools are within 1.5 km of the Kwun Tong air quality station? | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 18 |
| 11 | Which air quality station is closest to Victoria Park in Causeway Bay? | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 18 |
| 12 | How many hospitals are within 2 km of the Central/Western air quality station? | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 2/3 | 17 |
| 13 | What is the 95th percentile of hourly PM2.5 at tung chung in 2025? | 3/3 | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 17 |
| 14 | At causeway bay, what is the correlation between hourly NO2 and wind speed in 2025? | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | **1/3** | 1 |
| 15 | How many days in 2025 did Tai Po's daily average PM2.5 exceed 30? | 0/3 | **2/3** | 0/3 | **1/3** | 0/3 | **2/3** | 5 |
| 16 | What was the PM2.5 reading at the Tai Po air quality station at noon on 7 January 2025? | 3/3 | 2/3 | 3/3 | 2/3 | 3/3 | 2/3 | 15 |
| 17 | On Tai Po's worst PM2.5 day in 2025, what was the average wind speed at its paired weather station? | 3/3 | 2/3 | 2/3 | 0/3 | 3/3 | 1/3 | 11 |
| 18 | During the five hours with the highest PM2.5 at Tai Po in 2025, what was the average wind speed at the paired weather station? | 0/3 | **1/3** | 0/3 | 0/3 | 0/3 | 0/3 | 1 |
| 19 | How many schools are within 1.5 km of the air quality station with the highest annual average NO2 in 2025? | 3/3 | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 17 |
| 20 | On hours when the paired weather station measured wind below 2 m/s, what was the average NO2 at roadside stations minus general stations in 2025? | 0/3 | 0/3 | 0/3 | 0/3 | **1/3** | 0/3 | 1 |

## Answer to the two requested questions

**(a) Cases with 0 in every one of the 6 runs: none.**

**(b) Cases with 0 total across all runs: none** (same set as (a), so there is
no difference between the two definitions in these data).

The expectation in the prompt does not hold exactly. Each of #14, #18 and #20
is zero in 5 of 6 runs but has exactly one pass; #15 is zero in 3 of 6 runs and
has 5 passes:

| Case | Zero in N/6 runs | Total passes | The one / first non-zero run |
|---:|---:|---:|---|
| 14 | 5 | 1 | multi v4: `runs/multi/day9_v4_budget20_3x/cases/repeat-01-case-14.json:13,15` — `answer = -0.09086271870681067`, `pass = true` |
| 18 | 5 | 1 | multi v2: `runs/multi/day5b_v2_budget20_3x/cases/repeat-03-case-18.json:13,15` — `answer = 4.2406`, `pass = true` |
| 20 | 5 | 1 | single v4: `runs/baseline/day9_v4_budget20_3x/cases/repeat-03-case-20.json:13,15` — `answer = 31.180302891751722`, `pass = true` |
| 15 | 3 | 5 | multi v2 (2), multi v3 (1), multi v4 (2); e.g. `runs/multi/day5b_v2_budget20_3x/cases/repeat-01-case-15.json:13,15` — `answer = 31`, `pass = true` |

Because there are no all-zero cases, the detailed section below is supplied
for the four suspected cases (#14, #15, #18, #20), using the same fields the
task asked for.

## Detail for the four suspected cases

### Case 14 — correlation at Causeway Bay

Counts: sv2 0/3, mv2 0/3, sv3 0/3, mv3 0/3, sv4 0/3, mv4 1/3 (total 1/18).

Question — `evals/hk_baseline_v2.py:240-241`:

> At causeway bay, what is the correlation between hourly NO2 and wind speed
> in 2025?

Truth SQL — `evals/hk_baseline_v2.py:242-248` (verbatim concatenation of the
string literals):

```sql
SELECT round(corr(a.no2, w.wind_speed)::numeric, 2)
FROM hourly_station_air_quality a
JOIN air_quality_station s ON s.id = a.station_id
JOIN weather_station ws ON ws.station_code = 'wx_' || s.station_code
JOIN hourly_station_weather w ON w.station_id = ws.id AND w.datetime = a.datetime
WHERE s.name_en = 'Causeway Bay'
AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'
```

Truth value / tolerance — `evals/hk_baseline_v2.py:249-252`: `expected = -0.09`,
`kind = "float"`, `tolerance = 0.05`, `holdout = True`. Note at line 253:
"Fuzzy name plus a three-table hourly join."

Representative failing artifact —
`runs/multi/day5b_v2_budget20_3x/cases/repeat-01-case-14.json`, fields
`answer` (line 13) = `null`, `pass` (line 15) = `false`, `reason` (line 16) =
"ANSWER is unset", `code_blocks[0]` (line 39), `final_text` (line 56) =
"No data available for Causeway Bay in 2025." The first code block sets
`station_name = 'Tai Po'` (wrong station) and joins weather by the air-quality
id space:

```python
station_name = 'Tai Po'
sql = """
SELECT a.datetime, a.no2, w.wind_speed
FROM hourly_station_air_quality a
JOIN hourly_station_weather w ON a.station_id = w.station_id AND a.datetime = w.datetime
WHERE a.station_id = :station_id
  AND a.datetime >= '2025-01-01 00:00:00' AND a.datetime <= '2025-12-31 23:00:00'
"""
```

Why it is not all-zero: `runs/multi/day9_v4_budget20_3x/cases/repeat-01-case-14.json`
has `answer = -0.09086271870681067`, `pass = true` (lines 13, 15), produced by
`code_blocks[3]` (line 42), which uses `weather_station ws ON
ws.station_code = :weather_station_code` with `weather_station_code =
f'wx_{station_code}'` and `JOIN hourly_station_weather w ON w.station_id =
ws.id` (`code_blocks[2]`, line 41). Caveat: that file's `final_text` (line 54)
says "The station 'Causeway Bay' could not be uniquely resolved. The question
cannot be answered.", so the single pass is a staged numeric `ANSWER` that
contradicts the final prose. I am reporting the scorer fields, not claiming a
clean end-to-end success.

Judgment: **(a) single defensible reading, and the truth SQL implements it.**
The fuzzy name is deliberate (`evals/hk_baseline_v2.py:13-14,253`), and the
`'wx_' || station_code` pairing plus the ban on using the AQ `station_id` as a
weather `station_id` are written in the agent prompt
(`domains/hk/schema_prompt.py:171-178`). The observed failures are entity
resolution to the wrong station, direct-id joins, stale/unset ANSWER and the
`numpy.int64` bind error, not two competing readings of the question. Correlations
are Pearson via SQL `corr()`; the question does not name a coefficient, but I
found no evidence in the inspected artifacts of an alternative coefficient
being the intended one.

### Case 15 — days with daily-average PM2.5 above 30

Counts: sv2 0/3, mv2 2/3, sv3 0/3, mv3 1/3, sv4 0/3, mv4 2/3 (total 5/18).

Question — `evals/hk_baseline_v2.py:259`:

> How many days in 2025 did Tai Po's daily average PM2.5 exceed 30?

Truth SQL — `evals/hk_baseline_v2.py:260-267`:

```sql
WITH daily AS (
  SELECT a.datetime::date AS day, avg(a.pm2_5) AS v
  FROM hourly_station_air_quality a
  JOIN air_quality_station s ON s.id = a.station_id
  WHERE s.name_en = 'Tai Po'
  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'
  GROUP BY 1)
SELECT count(*) FROM daily WHERE v > 30
```

Truth value / tolerance — `evals/hk_baseline_v2.py:268-269`: `expected = 31`,
`kind = "int"`; tolerance is the `Case` default `0.0`
(`evals/hk_baseline_v2.py:42`).

Representative failing artifact —
`runs/baseline/day9_v4_budget20_3x/cases/repeat-01-case-15.json`, fields
`answer` (line 13) = `81`, `pass` (line 15) = `false`, `code_blocks[0]` (line 39),
`final_text` (line 44) = "81 days in 2025 did Tai Po's daily average PM2.5
exceed 30." The code counts days containing any hourly exceedance instead of
averaging first:

```sql
SELECT COUNT(DISTINCT DATE(datetime)) AS n_days
FROM hourly_station_air_quality hqa
JOIN air_quality_station s ON hqa.station_id = s.id
WHERE s.name_en = 'Tai Po'
  AND hqa.datetime >= '2025-01-01'
  AND hqa.datetime < '2026-01-01'
  AND hqa.pm2_5 > 30
```

The same 81-vs-31 pattern appears in all three single-v3 and all three
single-v4 repeats (case JSON `answer` fields). The prompt states the daily-mean
rule explicitly at `domains/hk/schema_prompt.py:179-180`.

Why it is not all-zero: e.g. `runs/multi/day5b_v2_budget20_3x/cases/repeat-01-case-15.json:13,15`
has `answer = 31`, `pass = true` (its `final_text` at line 59 is the identical-code
stop sentinel, so again the pass is the staged numeric value).

Judgment: **(a) single defensible reading, and the truth SQL implements it.**
"Days ... did ... daily average PM2.5 exceed 30" is explicit about the
grain: mean per day first, then count days. The 81 answers come from a
different computation (any-hour exceedance) documented as a known failure in
`docs/multiagent_failure_log.md:429`, not from an alternative reading of the
question.

### Case 18 — wind speed in the five highest-PM2.5 hours

Counts: sv2 0/3, mv2 1/3, sv3 0/3, mv3 0/3, sv4 0/3, mv4 0/3 (total 1/18).

Question — `evals/hk_baseline_v2.py:313-314`:

> During the five hours with the highest PM2.5 at Tai Po in 2025, what was the
> average wind speed at the paired weather station?

Truth SQL — `evals/hk_baseline_v2.py:315-326`:

```sql
WITH top5 AS (
  SELECT a.datetime
  FROM hourly_station_air_quality a
  JOIN air_quality_station s ON s.id = a.station_id
  WHERE s.name_en = 'Tai Po'
  AND a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'
  ORDER BY a.pm2_5 DESC NULLS LAST LIMIT 5)
SELECT round(avg(w.wind_speed)::numeric, 2)
FROM hourly_station_weather w
JOIN weather_station ws ON ws.id = w.station_id
JOIN top5 ON w.datetime = top5.datetime
WHERE ws.station_code = 'wx_taipo'
```

Truth value / tolerance — `evals/hk_baseline_v2.py:327-329`: `expected = 4.24`,
`kind = "float"`, `tolerance = 0.2`.

Representative failing artifact —
`runs/baseline/day5b_v3_budget20_3x/cases/repeat-01-case-18.json`, fields
`answer` (line 13) = `3.713`, `pass` (line 15) = `false`, `code_blocks[0]`
(line 39), `outputs[0]` (line 42). The code orders by PM2.5 descending without
`NULLS LAST`:

```sql
SELECT datetime, pm2_5
FROM hourly_station_air_quality
WHERE station_id = (SELECT id FROM air_quality_station WHERE station_code = :station_code)
  AND datetime >= '2025-01-01' AND datetime < '2026-01-01'
ORDER BY pm2_5 DESC
LIMIT 5
```

`outputs[0]` (line 42) shows all five selected rows have `pm2_5 = None`; the
agent still averages wind at those timestamps and gets 3.713, while its
`final_text` (line 44) claims the PM2.5 values are all NULL. The truth SQL's
`NULLS LAST` excludes missing readings. The prompt documents that NULL means
missing, never zero (`domains/hk/schema_prompt.py:181`), and the same v2 case
list contains a deliberate NULL trap (`evals/hk_baseline_v2.py:15-17`).

Why it is not all-zero: `runs/multi/day5b_v2_budget20_3x/cases/repeat-03-case-18.json:13,15`
has `answer = 4.2406`, `pass = true`.

Judgment: **(a) single defensible reading, and the truth SQL implements it.**
"Highest PM2.5" does not include hours with no PM2.5 measurement; the truth SQL
says so with `NULLS LAST`. The common 3.713 answer is a SQL NULL-ordering
mistake (PostgreSQL `DESC` sorts NULLs first), not a disagreement about what
the question asks. Caveat, stated plainly: the truth SQL has no deterministic
tie-break among equal PM2.5 values; I saw no evidence in the inspected runs
that a tie caused a failure.

### Case 20 — roadside minus general NO2 on low-wind hours

Counts: sv2 0/3, mv2 0/3, sv3 0/3, mv3 0/3, sv4 1/3, mv4 0/3 (total 1/18).

Question — `evals/hk_baseline_v2.py:354-355`:

> On hours when the paired weather station measured wind below 2 m/s, what was
> the average NO2 at roadside stations minus general stations in 2025?

Truth SQL — `evals/hk_baseline_v2.py:356-363`:

```sql
SELECT round((avg(a.no2) FILTER (WHERE s.station_type = 'ROADSIDE')
            - avg(a.no2) FILTER (WHERE s.station_type = 'GENERAL'))::numeric, 2)
FROM hourly_station_air_quality a
JOIN air_quality_station s ON s.id = a.station_id
JOIN weather_station ws ON ws.station_code = 'wx_' || s.station_code
JOIN hourly_station_weather w ON w.station_id = ws.id AND w.datetime = a.datetime
WHERE a.datetime >= '2025-01-01' AND a.datetime < '2026-01-01'
AND w.wind_speed < 2
```

Truth value / tolerance — `evals/hk_baseline_v2.py:364-367`: `expected = 31.18`,
`kind = "float"`, `tolerance = 0.2`, `holdout = True`.

Representative failing artifact —
`runs/baseline/day9_v4_budget20_3x/cases/repeat-01-case-20.json`, fields
`answer` (line 13) = `32.427832873887745`, `pass` (line 15) = `false`,
`code_blocks[0]` (line 39), `final_text` (line 44) = "The average difference in
NO2 levels between roadside and general stations on hours when the wind speed
was below 2 m/s in 2025 was 32.43 ug/m3." The code filters a shared weather
view by timestamp and averages paired row differences:

```sql
WITH filtered_hours AS (
    SELECT DISTINCT datetime
    FROM paired_hourly_weather
    WHERE wind_speed < 2
),
...
SELECT AVG(roadside.no2 - general.no2) AS avg_difference
FROM roadside_no2 roadside
JOIN general_no2 general ON roadside.datetime = general.datetime
```

This differs from the truth SQL in two ways: it applies the wind filter to any
weather row sharing the timestamp, and it computes the mean of row-wise
differences rather than the difference of the two group means. The existing
audit log describes this exact 32.43-vs-31.18 failure at
`docs/multiagent_failure_log.md:439` ("CTE filters every `wx_%` weather row with
wind below 2"). The same 32.43 answer recurs in mv3 repeat-03, mv4 repeats 01
and 02, and 32.43/32.31 in other repeats.

Why it is not all-zero: `runs/baseline/day9_v4_budget20_3x/cases/repeat-03-case-20.json:13,15`
has `answer = 31.180302891751722`, `pass = true`, with `final_text` (line 44)
stating 31.18 ug/m3.

Judgment: **(a) single defensible reading, and the truth SQL implements it,
with one small caveat.** "Average NO2 at roadside stations minus [average NO2
at] general stations" most naturally means difference of the two group means,
which is what the truth SQL computes; the agent's `AVG(roadside.no2 -
general.no2)` is a different statistic that needs an explicit station-pairing
rule the question does not give. The phrase "the paired weather station" is
not an unstated convention within this benchmark: the pairing rule is written
in the agent prompt at `domains/hk/schema_prompt.py:171-178`, and the
`paired_hourly_weather` view is also exposed in that prompt
(`domains/hk/schema_prompt.py:90,152`). The caveat is that the question never
spells out "difference of group means" verbatim; I do not regard that as a real
ambiguity, but it is the only one of the four cases where the wording could
have been tighter. No benchmark file was changed on this basis.

## Other honest caveats

- The single-v2 slot is a different run (day4) from the v3/v4 slots; its
  `summary.per_case` is the only evidence available for that slot, and it has
  no `cases/*.json` cross-check.
- Several passes are recorded from a staged numeric `ANSWER` while the case
  file's `final_text` is the identical-code stop sentinel; examples are
  `runs/multi/day5b_v2_budget20_3x/cases/repeat-01-case-15.json:13,15,59` and
  `runs/multi/day5b_v2_budget20_3x/cases/repeat-03-case-18.json:13,15,58`.
  These are counted as passes because `pass`/`pass_v2` is true in the artifact.
- On failing cases the `reason` field prints a comparison expression such as
  `"abs(81.0 - 31) < 0.5"` (`runs/baseline/day9_v4_budget20_3x/cases/repeat-01-case-15.json:16`)
  even though `pass` is false. This document reports the `pass` field, not the
  wording of `reason`.
