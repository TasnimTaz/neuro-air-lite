# Day 4–5 failure log: single-agent and hierarchical multi-agent

This log covers the v2 baseline defined in `evals/hk_baseline_v2.py` and run by
`evals/run_baseline.py`. It is written from the saved JSON artifacts, not from
memory. The single-agent prompt, questions and truth SQL were frozen before the
official Day 4 repeats; no prompt, question or truth SQL was changed after that
point. Day 5 reuses that frozen prompt for both arms and adds only the
multi-agent role/coordinator text in `domains/hk/roles.py`.

## Frozen reference

| Field | Value |
| --- | --- |
| Model | `qwen2.5-coder:14b` |
| Base URL | `http://127.0.0.1:11434/v1` |
| Prompt | 9,990 chars (~2,497 tokens) |
| Prompt sha256 | `ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048` |
| Cases | 20 (four per category; six hold-outs) |
| Repeats | 3 |
| Truth validation | 20/20 pinned values match the database |
| **20-step artifact (matched budget)** | `runs/baseline/day4_v2_budget20/run.json` (generated 2026-11-10T14:41:21Z) |
| 10-step artifact (historical Day 4 reference) | `runs/baseline/20261110_191651/run.json` (generated 2026-11-10T13:16:51Z) |

The official run used the unmodified v2 questions and truth SQL. The six
hold-outs are `#2, #6, #11, #14, #17, #20`.

The plan requires **20 total steps** for both the single and multi arms. The
20-step artifact is therefore the single-arm matched-budget reference for Day 9.
The 10-step artifact is retained as the historical Day 4 reference; it must not
be compared with a 20-step multi-agent arm.

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

## Results

### 20-step matched-budget reference

The 20-step run records **46/60 case-runs (76.7%)**, range **75.0%-80.0%** over
the three repeats. It is not a clean “20 steps improved the score” result: the
model took different paths, fixed `#3`, `#16` and `#17`, but regressed `#5` from
2/3 to 1/3. The prompt-level weather-join defect and the genuine aggregation
failures remain.

| Repeat | Passed | Hold-out | Non-hold-out | First attempt | Mean steps | Tokens | Wall-clock |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 15/20 | 4/6 | 11/14 | 20/20 | 2.75 | 189,771 | 239.3s |
| 2 | 16/20 | 4/6 | 12/14 | 18/20 | 2.85 | 203,110 | 272.8s |
| 3 | 15/20 | 4/6 | 11/14 | 19/20 | 2.80 | 195,631 | 243.2s |
| **Mean** | **76.7%** | **66.7%** | **81.0%** | **57/60** | **2.80** | **588,512** | **12.6s/case** |

| # | Category | Difficulty | Hold-out | Pass / 3 |
| ---: | --- | --- | :---: | ---: |
| 1 | retrieval | simple | no | 3/3 |
| 2 | retrieval | simple | yes | 3/3 |
| 3 | retrieval | medium | no | 3/3 |
| 4 | retrieval | simple | no | 3/3 |
| 5 | temporal | medium | no | 1/3 |
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
| 16 | statistics | medium | no | 3/3 |
| 17 | multi_table | hard | yes | 3/3 |
| 18 | multi_table | hard | no | 0/3 |
| 19 | multi_table | hard | no | 3/3 |
| 20 | multi_table | hard | yes | 0/3 |

By category: retrieval 100.0%, temporal 83.3%, spatial 100.0%, statistics
50.0%, multi_table 50.0%. By difficulty: simple 100.0%, medium 83.3%, hard
50.0%.

### 10-step preliminary artifact

The earlier 10-step artifact is kept for audit:

| Repeat | Passed | Hold-out | Non-hold-out | First attempt | Mean steps | Tokens | Wall-clock |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 16/20 | 4/6 | 12/14 | 18/20 | 3.15 | 266,960 | 455.2s |
| 2 | 15/20 | 4/6 | 11/14 | 18/20 | 2.65 | 184,983 | 252.6s |
| 3 | 13/20 | 3/6 | 10/14 | 20/20 | 2.60 | 179,163 | 213.2s |
| **Mean** | **73.3%** | **61.1%** | **78.6%** | **56/60** | **2.80** | **631,106** | **15.4s/case** |

The strict score was **44/60 (73.3%)**. A semantic audit counted two correct
NULL answers as passes (`#3` repeat 2 and `#16` repeat 3), giving 46/60 (76.7%).
The strict artifact was not rewritten after the audit, and the scorer was not
changed after the freeze. In the 20-step run both cases pass 3/3, so its strict
and semantic scores are both 46/60.

The 10-step per-case results were: #1 3/3, #2 3/3, #3 2/3, #4 3/3, #5 2/3,
#6 3/3, #7 3/3, #8 3/3, #9 3/3, #10 3/3, #11 3/3, #12 3/3, #13 3/3,
#14 0/3, #15 0/3, #16 2/3, #17 2/3, #18 0/3, #19 3/3, #20 0/3.

### Scorer note: two correct NULL answers in the 10-step run

The frozen `UNAVAILABLE` matcher accepts `not available`, `no data`,
`unavailable`, `missing` and `null`, but not every paraphrase:

* `#3`, repeat 2: the SQL returned NULL and the execution output said
  `CO concentration data is not available for Sha Tin in 2025.` The final text
  was `The question cannot be answered from the data.` (`ANSWER` unset).
* `#16`, repeat 3: the row exists and `pm2_5` is NULL; the final text was
  `No PM2.5 reading available for Tai Po at noon on 7 January 2025.`

Both answers are semantically correct. The 10-step strict score remains 44/60;
the semantic audit is **46/60 (76.7%)**. The 20-step run does not contain these
false negatives, so its 46/60 needs no semantic adjustment.

## Failure taxonomy

### 1. Prompt ambiguity and paired-weather join key

`hourly_station_air_quality.station_id` points at `air_quality_station.id`,
while `hourly_station_weather.station_id` points at `weather_station.id`. The id
spaces are different. The correct pairing is:

```sql
JOIN air_quality_station s ON s.id = a.station_id
JOIN weather_station ws
  ON ws.station_code = 'wx_' || s.station_code
JOIN hourly_station_weather w
  ON w.station_id = ws.id AND w.datetime = a.datetime
```

The prompt is part of the cause. Its domain rule says “join hourly tables on
`station_id`” (line 30), then separately says the weather code is
`'wx_<air quality station_code>'` (line 41) without a join example. The direct
join is a reasonable reading of line 30; this is a prompt ambiguity, not merely
a 14B capacity limit. The frozen v2 prompt was not changed, so the baseline
keeps measuring this defect. A v3/Day 6 prompt must make the rule explicit:
each hourly table joins its own station table on `station_id`; the two hourly
tables are paired through the station codes, never by `station_id`.

In the 20-step run, all three `#14` attempts use the direct join and return “No
data available for Causeway Bay in 2025”:

```sql
-- #14, all repeated code
JOIN hourly_station_weather w
  ON a.station_id = w.station_id AND a.datetime = w.datetime
```

The same direct join appears in `#18` repeat 2 and `#20` repeat 3. The prompt
fix is necessary but not sufficient: the other repeats fail for independent
reasons documented below.

`#5` is a different entity-resolution failure in the 20-step run. Repeat 1 uses
`lower(replace(name_en, ' ', '')) LIKE :station_name` but binds
`%Shatin%` with a capital `S`, so the lowercase comparison finds nothing.
Repeat 3 queries `weather_station.name_en LIKE '%Shatin%'` and misses
`Sha Tin` because of the space. Repeat 2 passes because it uses the lowercase
literal `%shatin%` and `wx_shatin`. The prompt rule is correct; the model did not
apply it consistently.

### 2. Top-five NULL ordering at Tai Po (#18, 0/3)

`#18` asks for the average wind speed during the five hours with the highest
PM2.5 at Tai Po. The truth orders with `NULLS LAST`:

```sql
ORDER BY a.pm2_5 DESC NULLS LAST
LIMIT 5
```

Two 20-step attempts (`r1`, `r3`) use a correct `wx_taipo` weather join but omit
`NULLS LAST`:

```sql
-- #18 r1
ORDER BY pm2_5 DESC
LIMIT 5
```

PostgreSQL's default is `NULLS FIRST` for `DESC`, so the five selected rows
include NULL PM2.5 hours; the mean wind at those hours is **3.71 m/s** instead
of the truth's **4.24 m/s**. `#18` repeat 2 instead uses the direct
`a.station_id = w.station_id` join and returns no data. This is a SQL-semantics
failure, not an arithmetic or model-capacity problem.

### 3. Roadside-minus-general aggregation (#20, 0/3)

`#20` asks for average roadside NO2 minus average general NO2, filtering each
station-hour by **that station's paired weather row** with wind below 2 m/s.

* Repeat 1 builds a `filtered_hours` set from *any* weather station with wind
  below 2, then applies that timestamp set to all AQ stations. It also writes
  `AVG(CASE WHEN station_type = 'ROADSIDE' THEN no2 ELSE 0 END)`, which changes
  the denominator and returns **-16.34** instead of 31.18.
* Repeat 2 attempts the correct AQ/weather pairing but joins through
  `hs.station_id`/`ws.station_id` aliases that do not exist in the CTE, producing
  `UndefinedColumn` and `UndefinedTable`; it retries the same broken shape.
* Repeat 3 joins the two hourly tables directly on `station_id`; the join is
  empty and the final text is “The question cannot be answered from the data.”

The truth uses one join keyed by both station code and datetime; the model's
three failures cover shared-timestamp filtering, alias/recovery, and the
prompt-level id-space confusion.

### 4. Aggregation grain: daily mean vs hourly exceedance (#15, 0/3)

`#15` asks for days whose **daily average** PM2.5 exceeded 30. The prompt states
the daily-mean rule explicitly (line 44) but has no worked CTE example. All three
20-step attempts count distinct dates with any individual hourly reading above
30:

```sql
SELECT COUNT(DISTINCT DATE(datetime))
FROM hourly_station_air_quality ...
WHERE ... AND pm2_5 > 30
```

That returns **81** days; the truth first averages the hourly values per day and
then counts **31** days. This is a wrong-grain error, not a weather-join error.
A corrected CTE example would make the pattern explicit, but the rule itself is
not contradictory.

### 5. Day flooring after choosing the worst day (#17)

The truth chooses the worst day by daily average PM2.5, then averages wind over
the whole calendar day. In the 10-step run, repeat 3 chose the worst day
correctly but returned `MAX(datetime)` for that day and used it as the start of
a one-day weather window:

```sql
SELECT MAX(datetime) AS worst_day ... ORDER BY AVG(pm2_5) DESC LIMIT 1
-- later: hsw.datetime >= :worst_day AND hsw.datetime < :worst_day + INTERVAL '1 day'
```

`worst_day` was 23:00, so the window covered only the last hour, producing
2.58 m/s instead of the full-day 1.81 m/s. In the 20-step run `#17` passes 3/3;
repeat 2 selects the day by maximum hourly PM2.5 rather than daily average, but
the day happens to match and the tolerance accepts 1.736 against 1.81. The
full-day pattern is therefore still a known weakness even though the case
passes.

### 6. Error recovery and step budget

The 10-step `#18` failure was the most expensive: repeat 1 spent all 10 steps
and 83,557 tokens repeating
`A value is required for bind parameter 'station_code'` and a failed
`datetime IN :datetimes` expansion, ending with
`[STEP_BUDGET_EXHAUSTED]`. The 20-step run did not repeat that loop; `#18` now
fails on the NULL-ordering and direct-join bugs instead. The 20-step budget
therefore helped one recovery path but did **not** fix the repeat-zero cases.
`#20` repeat 2 still spends five steps retrying invalid aliases. A verifier that
checks table aliases and join keys remains necessary.

### Calendar semantics observed in diagnostics, not repeat-zero

Pre-freeze r1 `#6` used PostgreSQL `EXTRACT(DOW)` with `dow IN (0,1,2,3,4)` as
“weekdays” (Sunday=0), returning 1.42 instead of 0.28. The official repeats all
pass `#6`, so this is variance/one bad SQL formulation, not a stable failure.
It is still a known semantic trap for Day 5's verifier.

## Hold-out failures

20-step hold-out score: **12/18 (66.7%)**. The six hold-outs are:

| Case | Question theme | Pass / 3 | Failure |
| ---: | --- | ---: | --- |
| 2 | general stations in new-town areas | 3/3 | none |
| 6 | Tung Chung weekend/weekday PM2.5 | 3/3 | diagnostic r1 only |
| 11 | closest station to Victoria Park | 3/3 | none |
| 14 | Causeway Bay NO2-wind correlation | 0/3 | paired-weather join key |
| 17 | Tai Po worst-day wind | 3/3 | 10-step r3 day flooring; 20-step passes |
| 20 | roadside minus general NO2, low wind | 0/3 | paired-weather join/aggregation |

The hold-out failures are concentrated in the same multi-table weather joins
as the non-hold-out failures; there is no evidence that the prompt was tuned
to the hold-outs. Two of the six hold-outs (`#14` and `#20`) are repeat-zero in
both budgets, so the hold-out rate remains lower than the overall rate even
though spatial questions are 100%.

## What this does and does not show

- **This is a single-agent reference, not a multi-agent result.** No claim of a
  multi-agent gain can be made from 44/60 or 46/60. The Day 9 arm must run the
  same frozen v2 set, the same prompt and the same scorer; otherwise the
  comparison is invalid.
- **The 20-step artifact is the matched-budget single-arm reference.** It uses
  the same frozen prompt and questions, passes 20/20 truth validation, and is the
  correct starting point for a 20-step multi-agent arm. The 10-step artifact is
  historical and must not be compared with a 20-step multi arm.
- **The 20-step score is 46/60 strict and 46/60 semantic.** The 10-step score is
  44/60 strict and 46/60 semantic. The raw +2 in the 20-step strict score is not
  a clean budget effect: `#3`, `#16` and `#17` pass, but `#5` regresses from 2/3
  to 1/3, and the path randomness is visible in the per-repeat results.
- **The prompt ambiguity remains the dominant failure family.** Three of the four
  repeat-zero cases (`#14`, `#18`, `#20`) plus one `#5` miss involve the separate
  AQ and weather id spaces or entity resolution. A verifier that checks the join
  key, or a prompt example that demonstrates `wx_<station_code>`, is the obvious
  Day 5/Day 6 target.
- **The prompt fix alone will not remove every failure.** `#18` also needs
  `NULLS LAST`; `#20` needs paired-row aggregation rather than a shared timestamp
  set and correct aliases; `#15` needs a daily-mean CTE. These are separate
  SQL-semantics and recovery failures.
- **Day-grain and aggregation errors remain.** `#15` and `#20` show that “daily
  average”, “five highest” and “roadside minus general” still need explicit
  grain/CTE patterns; this is not a single model-capacity ceiling.
- **Cost is concentrated but lower at 20 steps in this sample.** The 20-step run
  used 588,512 tokens across 60 case-runs (12.6s/case), versus 631,106
  (15.4s/case) at 10 steps. `#18` alone used 28,220 tokens across three repeats;
  `#20` used 44,518. A step-budget/cost table should accompany any later
  multi-agent comparison.
- **Environment and scope.** Results are from one local model
  (`qwen2.5-coder:14b`, Ollama, temperature 0.2), three repeats, one machine.
  The host suspended once during an earlier 3-repeat attempt; that attempt is
  not used. No clean-checkout or Docker-only reproduction was run for these
  tables beyond the separate test suite.
- **Not verified here:** a corrected text matcher, a later model, a multi-agent
  arm, or a fresh-prompt v3. All are open for Day 5/Day 9.

## Day 5: hierarchical multi-agent

### Protocol

The multi arm uses the same 20 v2 questions, truth SQL, model, frozen domain
prompt and strict scorer as the single arm. The coordinator produces a JSON plan
of at most six steps; the last step is forced to `synthesis_report`. Workers get
fresh chat histories and share one `PythonRuntime`. The fixed solver budget is
20 steps: 11 producer, 3 synthesis reserve, 6 verifier reserve. The coordinator's
planning call is recorded separately as one call and is not charged to the 20
solver steps (see the all-call caveat below). The multi prompt hash is
`141925a0538b2e039cb0d4ac78589ff0ddd3a99e3e5a27b5cd322976289c23a1`; the frozen
domain prompt hash remains
`ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048`.

The multi arm writes `runs/multi/<timestamp>/run.json` and
`multi_prompts.txt`. Every worker entry records its role, task, allocated
budget, actual steps, tokens, first-attempt status, executions and final text;
the case also records the plan, `plan_source`, `solver_steps`, `calls`,
`planner_tokens` and the verifier text. A truth mismatch stops the run. The
1-repeat artifact is the usable comparison. A 3-repeat attempt with the same
prompt hash ended during repeat 1 at `#19`; it produced no `run.json`, so its
score is not reported. Its log is
`runs/multi/day5_v2_budget20_3x.log` (last write 2026-11-10T23:25 local); the
process had already exited when checked, with no error line after `[19/20]`.

### Usable 1-repeat

`runs/multi/day5_v2_budget20_r1/run.json` (generated
2026-11-10T16:15:58Z) scored **11/20 strict (55.0%)** and **13/20 semantic** in a
manual audit. The two semantic additions are `#3` and `#16`: both returned the
correct unavailable value with wording the strict matcher does not accept
(`no CO data available`, `No PM2.5 reading available`), not a wrong number.

| Arm | Strict | Semantic | Hold-out | Mean solver steps | Calls | Tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Single v2, 20 steps, 3 repeats | 46/60 (76.7%) | 46/60 | 12/18 (66.7%) | 2.8 | 168 | 588,512 |
| Multi v2, 1 repeat | 11/20 (55.0%) | 13/20 | 3/6 (50.0%) | 11.3 | 246 | 978,423 |

The single row is 60 case-runs; the multi row is 20. This is a signal that the
multi arm is worse, not a like-for-like repeat-count comparison. A 3-repeat
multi run is still required for the Day 9 table.

The multi run used 177 producer steps, 49 verifier steps and 20 planner
calls (9,456 planner tokens). It added no case that the single arm failed and
regressed on `#3`, `#4`, `#16` and `#17`. The four repeat-zero single-arm cases
(`#14`, `#15`, `#18`, `#20`) and `#5` still failed.

### Failure taxonomy

1. **Synthesis refusal despite staged values.** The synthesis worker often
   returned "cannot be answered" while the upstream workers had already staged
   the required value. The verifier rescued several cases (`#2`, `#6`, `#7`,
   `#9`, `#10`, `#12`, `#13`) but not all. The orchestrator passes only the
   variable manifest to the next worker, not the previous worker's final text;
   this is the leading Day 6 coordination target.
2. **Verifier contract gaps at the text boundary.** `#3` and `#16` were correct
   but failed the strict wording rule. `#4` combined a correct "not available"
   sentence with a stale `ANSWER` from synthesis, so the numeric/unanswerable
   branch failed. The verifier must explicitly unset `ANSWER` when the premise
   is invalid.
3. **Verifier entity re-resolution and stalls.** `#5`, `#15`, `#17` and `#18`
   show failures where the verifier re-ran a broad name lookup, got multiple
   matches, or stopped because it repeated a code block. The stall guard saved
   budget but could not create a correct value.
4. **The frozen weather-join ambiguity remains.** `#5`, `#14`, `#18` and `#20`
   still use the separate AQ/weather id spaces without the `wx_` pairing. The
   multi arm uses the same schema prompt, so the verifier inherits the same
   ambiguity; this is not a topology bug.
5. **Aggregation grain and recovery.** `#15` returned 30 instead of 31 (the
   daily-mean exceedance grain); `#14`'s synthesis value was 3.51 but the
   verifier discarded it; `#18` returned a date rather than a numeric value.
6. **Cost.** The multi arm used roughly 4.4x the single arm's calls and 5x its
   tokens for one repeat, while consuming 11.3 of 20 solver steps on average.
   The coordinator's 20 calls are only 9,456 tokens, so the extra cost is
   dominated by worker/verifier execution, not planning.

### All-call caveat and acceptance

The PDF's Day 5 acceptance condition is `multi >= single`; the multi run does
not meet it. The plan says to fix this in Day 6, not to tune the benchmark.
A 19-step solver sensitivity run (charging the planner) and a v3 prompt that
states the weather join are still required before any Day 9 claim. The strict
scorer was not changed; the semantic count is an audit, not an official score.
