# Day 4 failure log: single-agent Hong Kong baseline

This log covers the v2 baseline defined in `evals/hk_baseline_v2.py` and run by
`evals/run_baseline.py`. It is written from the saved JSON artifacts, not from
memory. The prompt was frozen before the official three repeats; no prompt,
question or truth SQL was changed after that point.

## Frozen reference

| Field | Value |
| --- | --- |
| Model | `qwen2.5-coder:14b` |
| Base URL | `http://127.0.0.1:11434/v1` |
| Prompt | 9,990 chars (~2,497 tokens) |
| Prompt sha256 | `ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048` |
| Cases | 20 (four per category; six hold-outs) |
| Repeats | 3 |
| Max steps | 10 (Day 4 reference; the Day 5/9 matched budget is 20) |
| Truth validation | 20/20 pinned values match the database |
| Official artifact | `runs/baseline/20261110_191651/run.json` (generated 2026-11-10T13:16:51Z) |

The official run used the unmodified v2 questions and truth SQL. The six
hold-outs are `#2, #6, #11, #14, #17, #20`.

## Pre-freeze diagnostics (not the official table)

Before freezing, two 1-repeat diagnostic runs used the previous prompt
(`43bbaa48...`). They exposed two deterministic instruction defects, not model
randomness. Both were corrected generically **before** the official repeats;
the v2 questions and truth SQL were not touched.

| Diagnostic | Passed | What failed |
| --- | --- | --- |
| pre-freeze r1 | 11/20 | #5, #6, #9, #10, #14, #15, #18, #19, #20 |
| pre-freeze r2 | 12/20 | #5, #9, #10, #14, #15, #18, #19, #20 |

### Defect 1: the fuzzy-name example contradicted the data

The prompt told the model to search
`lower(name_en) LIKE '%shatin%'` for `Shatin`. The canonical value is
`Sha Tin` (with a space), so the pattern returns zero rows. The model followed
the instruction and then (in r1) raised `NoResultFound`:

```sql
SELECT id FROM air_quality_station
WHERE lower(name_en) LIKE '%shatin%'
```

Database check:

| Pattern | Match |
| --- | --- |
| `%shatin%` | none |
| `%sha tin%` | `Sha Tin` |
| `lower(replace(name_en, ' ', '')) LIKE '%shatin%'` | `Sha Tin` |

The rule now normalizes spacing before the `LIKE`; `#5` is answerable again
without changing the question. This defect alone made `#5` fail deterministically
in both diagnostics.

### Defect 2: the worked school example used the wrong source

The prompt's school example counted `osm_feature` rows with
`f.sub_type = 'school'`, while the v2 truth counts the official
`sensitive_area` inventory (`EDUCATIONAL_INSTITUTION`). The model followed the
example and returned 15, 43 and 43 for `#9`, `#10` and `#19` instead of
21, 51 and 56. This is a source-of-truth ambiguity, not an arithmetic error.
The example now uses `sensitive_area`, and a rule states that `osm_feature` is
general map data, not the authoritative institution inventory.

The diagnostic runs remain on disk under `runs/pre_freeze_prompt_bug/` for
audit, but they are **not** part of the official table.

## Official results

The frozen artifact records **44/60 case-runs (73.3%)**, range **65.0%-80.0%**
over the three repeats. The strict scorer is deliberately left unchanged.

| Repeat | Passed | Hold-out | Non-hold-out | First attempt | Mean steps | Tokens | Wall-clock |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 16/20 | 4/6 | 12/14 | 18/20 | 3.15 | 266,960 | 455.2s |
| 2 | 15/20 | 4/6 | 11/14 | 18/20 | 2.65 | 184,983 | 252.6s |
| 3 | 13/20 | 3/6 | 10/14 | 20/20 | 2.60 | 179,163 | 213.2s |
| **Mean** | **73.3%** | **61.1%** | **78.6%** | **56/60** | **2.80** | **631,106** | **15.4s/case** |

| # | Category | Difficulty | Hold-out | Pass / 3 |
| ---: | --- | --- | :---: | ---: |
| 1 | retrieval | simple | no | 3/3 |
| 2 | retrieval | simple | yes | 3/3 |
| 3 | retrieval | medium | no | 2/3 |
| 4 | retrieval | simple | no | 3/3 |
| 5 | temporal | medium | no | 2/3 |
| 6 | temporal | medium | yes | 3/3 |
| 7 | temporal | medium | no | 3/3 |
| 8 | temporal | simple | no | 3/3 |
| 9 | spatial | medium | no | 3/3 |
| 10 | spatial | medium | no | 3/3 |
| 11 | spatial | hard | yes | 3/3 |
| 12 | spatial | medium | no | 3/3 |
| 13 | statistics | medium | no | 3/3 |
| 14 | statistics | hard | yes | 0/3 |
| 15 | statistics | medium | no | 0/3 |
| 16 | statistics | medium | no | 2/3 |
| 17 | multi_table | hard | yes | 2/3 |
| 18 | multi_table | hard | no | 0/3 |
| 19 | multi_table | hard | no | 3/3 |
| 20 | multi_table | hard | yes | 0/3 |

By category: retrieval 91.7%, temporal 91.7%, spatial 100.0%, statistics
41.7%, multi_table 41.7%. By difficulty: simple 100.0%, medium 80.0%, hard
44.4%.

### Scorer note: two correct NULL answers counted as failures

The frozen `UNAVAILABLE` matcher accepts `not available`, `no data`,
`unavailable`, `missing` and `null`, but not every paraphrase:

* `#3`, repeat 2: the SQL returned NULL and the execution output said
  `CO concentration data is not available for Sha Tin in 2025.` The final text
  was `The question cannot be answered from the data.` (`ANSWER` unset). The
  strict matcher counts this as a miss.
* `#16`, repeat 3: the row exists and `pm2_5` is NULL; the final text was
  `No PM2.5 reading available for Tai Po at noon on 7 January 2025.` The strict
  matcher counts this as a miss.

Both answers are semantically correct. A semantic audit would add these two
passes: **46/60 (76.7%)**, with per-repeat 16/16/14 and range 70.0%-80.0%.
The artifact remains at the strict 44/60 because changing the scorer after the
freeze would silently move the reference. The matcher should be fixed before
the next baseline or both scores should be reported explicitly.

## Failure taxonomy

### 1. Paired-weather join key (repeat-zero: #14, #18, #20; one miss: #5)

This is the dominant failure family. `hourly_station_air_quality.station_id`
points at `air_quality_station.id`, while `hourly_station_weather.station_id`
points at `weather_station.id`. The id spaces are different. The prompt's rule
is to pair them through `weather_station.station_code = 'wx_' || aq.station_code`,
but the model repeatedly joins the two hourly tables on `station_id` directly:

```sql
-- repeat 1, #14
JOIN hourly_station_weather w
  ON a.station_id = w.station_id AND a.datetime = w.datetime
```

**The prompt is part of the cause.** Its domain rule says "join hourly tables
on `station_id`" (line 30), then separately says the weather code is
`'wx_<air quality station_code>'` (line 41) without a join example. The direct
join is a reasonable reading of line 30; this is a prompt ambiguity, not merely
a 14B capacity limit. The frozen v2 prompt was not changed, so the baseline
keeps measuring this defect. A v3/Day 6 prompt must make the rule explicit:
each hourly table joins its own station table on `station_id`; the two hourly
tables are paired through the station codes, never by `station_id`.

The join is empty, so `#14` finishes with "No data available for Causeway Bay
in 2025." The same mistake appears in `#18` (repeat 2) and `#20` (repeat 3);
`#5` (repeat 3) uses the AQ id when querying `hourly_station_weather`, so the
August/January weather rows disappear and the model reports "data ... is not
available." `#5` passes in repeats 1-2 because those attempts use the
`wx_` pairing.

The prompt fix is necessary but not sufficient: `#18` repeat 1 did use the
`wx_` code and still failed on `datetime IN :datetimes` (bind expansion) and the
10-step budget; `#20` repeat 2 paired the station correctly but filtered all
stations by a shared timestamp set, producing 30.31 instead of 31.18. Those are
recovery and aggregation failures on top of the join ambiguity.

`#20` also shows two distinct aggregation mistakes:

* repeat 2 uses a set of timestamps where *any* station had wind below 2 m/s,
  then averages roadside and general NO2 over that timestamp set. The truth
  filters each station-hour by **that station's paired weather row**; the
  result is 30.31 instead of 31.18.
* repeat 3 joins the AQ id to the weather id (empty), while repeat 1 uses
  non-existent aliases (`ws.station_id`, `a.station_id`) and ends with
  "The question cannot be answered".

### 2. Aggregation grain: daily mean vs hourly exceedance (#15, 0/3)

`#15` asks for days whose **daily average** PM2.5 exceeded 30. The prompt states
the daily-mean rule explicitly (line 44) but has no worked CTE example. Repeats
1 and 3 count distinct dates with any individual hourly reading above 30:

```sql
SELECT COUNT(DISTINCT DATE(datetime))
FROM hourly_station_air_quality ...
WHERE ... AND hqaq.pm2_5 > 30
```

That returns 81 days; the truth first averages the hourly values per day and
then counts 31 days. Repeat 2 tries
`AVG(...) OVER (PARTITION BY DATE(datetime))` inside `WHERE`, gets
`WindowingError: window functions are not allowed in WHERE`, and stops without
retrying. The three failures are therefore two different bugs: wrong grain in
two runs, and a failed self-repair in the third.

### 3. Day flooring after choosing the worst day (#17, one miss in repeat 3)

The truth chooses the worst day by daily average, then averages wind over the
whole calendar day. Repeat 3 chooses the worst day correctly but returns
`MAX(datetime)` for that day and uses it as the start of a one-day weather
window:

```sql
SELECT MAX(datetime) AS worst_day ... ORDER BY AVG(pm2_5) DESC LIMIT 1
-- later: hsw.datetime >= :worst_day AND hsw.datetime < :worst_day + INTERVAL '1 day'
```

`worst_day` is 23:00, so the window covers only the last hour, producing 2.58
m/s instead of the full-day 1.81 m/s. This is the same day-flooring failure
seen in v1 case 1 (`max(datetime)` instead of a date floor), not a spatial or
weather-pairing error. Repeats 1-2 pass.

### 4. Text/finalization mismatch on NULL answers (#3, #16; scorer limitation)

These are the two semantics-correct answers described in the scorer note above.
In both cases the model's SQL and execution output are right; the final prose
uses a paraphrase (`cannot be answered`, `No ... available`) that the frozen
matcher does not accept. They are **not** evidence that the model misread the
NULL data.

### 5. Error recovery and step budget (#18, #20)

`#18` is the most expensive failure: repeat 1 spends all 10 steps and 83,557
tokens repeating essentially the same code, with
`A value is required for bind parameter 'station_code'` and a failed attempt
to expand `datetime IN :datetimes`. It never uses the `wx_taipo` join and ends
with `[STEP_BUDGET_EXHAUSTED]`. The three repeats average 4.7 steps, 97,745
tokens and 257.4 seconds for this case alone. This is a recovery/orchestration
failure distinct from the arithmetic failures above.

### Calendar semantics observed in diagnostics, not repeat-zero

Pre-freeze r1 #6 used PostgreSQL `EXTRACT(DOW)` with `dow IN (0,1,2,3,4)` as
"weekdays" (Sunday=0), returning 1.42 instead of 0.28. The official repeats all
pass #6 (0.276, 0.28, 0.283), so this is variance/one bad SQL formulation, not
a stable failure. It is still a known semantic trap for Day 5's verifier.

## Hold-out failures

Hold-out score: **11/18 (61.1%)**. The six hold-outs are:

| Case | Question theme | Pass / 3 | Failure |
| ---: | --- | ---: | --- |
| 2 | general stations in new-town areas | 3/3 | none |
| 6 | Tung Chung weekend/weekday PM2.5 | 3/3 | diagnostic r1 only |
| 11 | closest station to Victoria Park | 3/3 | none |
| 14 | Causeway Bay NO2-wind correlation | 0/3 | paired-weather join key |
| 17 | Tai Po worst-day wind | 2/3 | day flooring in r3 |
| 20 | roadside minus general NO2, low wind | 0/3 | paired-weather join/aggregation |

The hold-out failures are concentrated in the same multi-table weather joins
as the non-hold-out failures; there is no evidence that the prompt was tuned
to the hold-outs. Two of the six hold-outs (#14 and #20) are repeat-zero, so
the hold-out rate is lower than the overall rate even though spatial questions
are 100%.

## What this does and does not show

- **This is a single-agent reference, not a multi-agent result.** No claim of a
  multi-agent gain can be made from 44/60 or 46/60. The Day 9 arm must run the
  same frozen v2 set and the same scorer; otherwise the comparison is invalid.
- **The strict score is 44/60.** The two scorer false negatives are documented
  above; the semantic audit is 46/60 (76.7%). The frozen artifact was not
  rewritten after the audit.
- **The two prompt fixes were pre-freeze.** They were derived from non-hold-out
  cases #5, #9, #10 and #19, not from the six hold-outs. No v2 question or truth
  SQL changed. The prompt hash is recorded in every artifact.
- **The main remaining weakness is the paired-weather join.** Three of the four
  repeat-zero cases (#14, #18, #20) plus one #5 miss involve the separate AQ and
  weather id spaces. A verifier that checks the join key, or a prompt example
  that demonstrates `wx_<station_code>`, is the obvious Day 5 target.
- **Day-grain and aggregation errors remain.** `#15` and `#17` show that
  "daily average" and "worst day" still need an explicit day-flooring/CTE
  pattern; this is not a model-capacity ceiling.
- **The step budget is not the Day 9 matched budget.** The plan specifies 20
  total steps for both the single and multi arms. This Day 4 artifact used 10,
  so it is a valid Day 4 reference but **not** a fair Day 9 comparison baseline.
  `evals/run_baseline.py` now defaults to 20; rerun v2 at 20 before Day 9 (or run
  both arms at 10). The 10-step table must not be compared with a 20-step
  multi-agent arm.
- **Cost is concentrated.** `#18` alone consumed 97,745 tokens across three
  repeats and 257.4 seconds per attempt, including one 10-step budget exhaustion.
  A step-budget/cost table should accompany any later multi-agent comparison.
- **Environment and scope.** Results are from one local model
  (`qwen2.5-coder:14b`, Ollama, temperature 0.2), three repeats, one machine.
  The host suspended once during an earlier 3-repeat attempt; that attempt is
  not used. No clean-checkout or Docker-only reproduction was run for this
  table beyond the separate test suite.
- **Not verified here:** a corrected text matcher, a later model, a multi-agent
  arm, or a fresh-prompt v3. All are open for Day 5/Day 9.
