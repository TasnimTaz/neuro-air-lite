# Day 5b: multi-agent measurement, verifier audit and failures

This is the official Day 5b multi-agent record. It supersedes the 1-repeat Day 5
signal in `docs/failure_log.md`. The single-arm reference is the matched 20-step
v2 run in `runs/baseline/day4_v2_budget20/run.json`; both arms use the same 20
questions, truth SQL, model and base prompt.

The v2 result below is frozen. A pre-registered v3 base-prompt rerun is recorded
at the end of this file; it does **not** rewrite the v2 artifact, scorer or
failure log. The v3 single arm is a fairness control, not a replacement for the
frozen v2 single reference.

## Protocol

| Item | Value |
| --- | --- |
| Multi artifact | `runs/multi/day5b_v2_budget20_3x/run.json` |
| Model | `qwen2.5-coder:14b` |
| Base prompt sha256 | `ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048` |
| Multi addendum sha256 | `141925a0538b2e039cb0d4ac78589ff0ddd3a99e3e5a27b5cd322976289c23a1` |
| Solver budget | 20 steps + 1 coordinator call |
| Producer / synthesis / verifier reserve | 11 / 3 / 6 |
| Repeats | 3, one attempt per task per repeat |
| Primary scorer | v2 (v1 and semantic stored per case) |
| Verifier mode | `overwrite` |

The base domain prompt is the byte-identical prefix of every worker system
prompt. The multi arm appends a delimited role addendum and the shared runtime
rules; it adds procedure, not schema or data facts. The Day 5 worker prompt put
the role paragraph first; Day 5b moves the base prompt to the prefix so the
fairness contract is structural, not just a claim. `multi_prompts_sha256`
hashes the addendum/role text, which is unchanged; the base-first assembly is
covered by a fake-LLM test. The prompt-leakage tests cover the v2 questions,
truth values and station codes; the v3 extension covers the explicit
weather-join rule.

Every `(case, repeat)` is written atomically in `cases/`; `--resume` skips
finished files. `answer_pre_verify` and `final_text_pre_verify` are snapshotted
before the verifier, so the same artifact contains a free “multi without
verifier” ablation. The runner is invoked with the default `overwrite` verifier
mode; the `flag` and `none` modes are for the later controlled comparison. The
official process exited after 34/60 saved case files; the resume lost only the
in-flight case (`repeat 2, #15`) and rebuilt `run.json` with `resumed: true`.

### Scorer freeze and the aborted first attempt

The first Day 5b 3-repeat attempt was stopped after four cases. Its scorer v2
missed the pre-registered `could not` phrase, so case `#4` (a correct
“could not be uniquely identified” refusal) was scored as a failure. The
partial artifact is retained at
`runs/multi/day5b_v2_budget20_3x_pre_scorer_fix/` with only four case files and no
`run.json`; it is not an official result. After adding that phrase from the
pre-registered v2 list, the scorer was frozen and the official run was restarted
from case `#1`. No threshold, numeric rule or case-specific exception was
changed.

## Results

The official artifact contains 60 case-runs per arm (20 tasks × 3 repeats). The
multi process exited after 34 saved case files and was resumed once; no case was
rerun twice and the in-flight case was rerun once. The final artifact records
`resumed: true`.

| Arm | v1 | v2 | Semantic | Repeats | Hold-out | Non-hold-out | Tokens | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Single, 20 steps | 46/60 (76.7%) | 46/60 (76.7%) | 46/60 | 15/16/15 | 12/18 (66.7%) | 34/42 (81.0%) | 588,512 | 755.3 |
| Multi, 20 steps + planner | 37/60 (61.7%) | **41/60 (68.3%)** | 41/60 | 15/13/13 | 11/18 (61.1%) | 30/42 (71.4%) | 3,065,202 | 6,877.5 |

The multi arm is **5 case-runs lower** than the single arm on the primary v2
scorer (41 vs 46). The scorer-v2 difference is the four correct no-data cases
(`#3` in all three repeats and `#16` in repeat 3); v1 scores the same artifact
37/60. Scorer v2 adds no false positives here.

| Arm | Mean steps | Calls | First attempt | Tokens/case | Seconds/case | Stalls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Single | 2.80 | 168 | 57/60 | 9,809 | 12.6 | 0 |
| Multi | 11.47 | 748 | 45/60 | 51,087 | 114.6 | 34 |

The multi arm used 688 solver steps (525 producer + 163 verifier) plus 60
planner calls and 28,687 planner tokens. Relative cost per case is **5.21×
tokens, 9.11× wall-clock and 4.45× calls**. The planner call is still separate
from the 20-step solver cap, so these are lower bounds on an all-call budget.

### Category and difficulty

| Group | Single | Multi |
| --- | ---: | ---: |
| retrieval | 12/12 (100%) | 11/12 (91.7%) |
| temporal | 10/12 (83.3%) | 7/12 (58.3%) |
| spatial | 12/12 (100%) | 12/12 (100%) |
| statistics | 6/12 (50.0%) | 6/12 (50.0%) |
| multi_table | 6/12 (50.0%) | 5/12 (41.7%) |
| simple | 12/12 (100%) | 10/12 (83.3%) |
| medium | 25/30 (83.3%) | 23/30 (76.7%) |
| hard | 9/18 (50.0%) | 8/18 (44.4%) |

The losses are concentrated in temporal and multi-table questions; spatial is
unchanged and statistics is flat. The hold-out difference is one case-run
(11/18 vs 12/18), not clean generalization evidence.

### Per-case counts

| # | Category | Difficulty | Hold-out | Single | Multi | Delta |
| ---: | --- | --- | :---: | ---: | ---: | ---: |
| 1 | retrieval | simple | no | 3/3 | 3/3 | +0 |
| 2 | retrieval | simple | yes | 3/3 | 3/3 | +0 |
| 3 | retrieval | medium | no | 3/3 | 3/3 | +0 |
| 4 | retrieval | simple | no | 3/3 | 2/3 | -1 |
| 5 | temporal | medium | no | 1/3 | 0/3 | -1 |
| 6 | temporal | medium | yes | 3/3 | 3/3 | +0 |
| 7 | temporal | medium | no | 3/3 | 2/3 | -1 |
| 8 | temporal | simple | no | 3/3 | 2/3 | -1 |
| 9 | spatial | medium | no | 3/3 | 3/3 | +0 |
| 10 | spatial | medium | no | 3/3 | 3/3 | +0 |
| 11 | spatial | hard | yes | 3/3 | 3/3 | +0 |
| 12 | spatial | medium | no | 3/3 | 3/3 | +0 |
| 13 | statistics | medium | no | 3/3 | 2/3 | -1 |
| 14 | statistics | hard | yes | 0/3 | 0/3 | +0 |
| 15 | statistics | medium | no | 0/3 | 2/3 | +2 |
| 16 | statistics | medium | no | 3/3 | 2/3 | -1 |
| 17 | multi_table | hard | yes | 3/3 | 2/3 | -1 |
| 18 | multi_table | hard | no | 0/3 | 1/3 | +1 |
| 19 | multi_table | hard | no | 3/3 | 2/3 | -1 |
| 20 | multi_table | hard | yes | 0/3 | 0/3 | +0 |

### Paired task-level test

Repeats are not independent. The task-level majority comparison is **2 wins, 8
losses, 10 ties** (wins `#15`, `#18`; losses `#4`, `#5`, `#7`, `#8`, `#13`,
`#16`, `#17`, `#19`; ties `#1`, `#2`, `#3`, `#6`, `#9`, `#10`, `#11`, `#12`,
`#14`, `#20`). The exact McNemar/binomial test gives **p=0.1094**; the paired
task bootstrap mean difference is **−0.083**, 95% CI **[-0.183, +0.033]**
(5,000 task resamples, seed 20261114). The point estimate is negative, but the
task-level test does not reject the null; the honest conclusion is **no evidence
of a multi-agent gain**, not a proven significant loss. The 5.21× token and 9.11×
time costs make the practical result negative even though 20 tasks give low
power.

## Verifier audit

`overwrite` is doing most of the work. The synthesis result before the verifier
passes only **13/60 (21.7%)**; the final multi result is **41/60 (68.3%)**. The
verifier changed `ANSWER` in 37/60 case-runs.

| Repeat | Pre-verify | Final | Wrong→right | Right→wrong | Right→right | Wrong→wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 5/20 | 15/20 | 10 | 0 | 5 | 5 |
| 2 | 5/20 | 13/20 | 8 | 0 | 5 | 7 |
| 3 | 3/20 | 13/20 | 10 | 0 | 3 | 7 |
| **Total** | **13/60** | **41/60** | **28** | **0** | **13** | **19** |

The 28 wrong→right rescues are real: without verifier overwrite, this multi
topology would be far below the single arm. But the verifier does not close the
gap: 19 case-runs remain wrong, and the verifier never turns a scored pre-pass
into a post-fail (0 right→wrong by the stored pass flag).

That zero needs a caveat. Case `#16` repeat 1 had a correct pre-verifier
sentence, “No PM2.5 data available”, but its pre-verifier `ANSWER` was `NaN`, so
the pre-pass flag was already false. The verifier replaced the sentence with
“Tai Po … does not resolve to exactly one station” and left `ANSWER=NaN`; this
is a text-quality regression that the right→wrong count does not capture. The
same artifact also contains verifier-written non-finite values (`#18` repeat 1,
`#20` repeat 3) and `#14` repeat 2, where the verifier put the string
`"No data available"` into a numeric `ANSWER`. The verifier contract needs an
explicit “unset ANSWER on invalid premise / never write a string or NaN” rule.

The pre-verifier pass is a conservative lower bound: several pre texts were
plausible refusals with `ANSWER` unset or `NaN`. It still demonstrates the main
attribution point: the final multi score is a **worker-plus-verifier** result,
not evidence that the worker handoff alone works.

## Failure taxonomy

All **19 failing case-runs** are classified below. Overlaps are possible; the
“primary” column names the first blocker, not the only one.

| Repeat | Case | Primary class | Evidence |
| ---: | ---: | --- | --- |
| 1 | 5 | weather pairing + stall | Both temperature queries refused; 2 stalls; sentinel final with `ANSWER` unset. |
| 1 | 14 | weather join / wrong column | Direct hourly join used `station_code` on the air-quality hourly table; final refusal, no numeric answer. |
| 1 | 16 | non-finite + verifier text | Pre sentence said “No PM2.5 data” but `ANSWER=NaN`; verifier changed it to an ambiguous-station message and kept `NaN`. |
| 1 | 18 | weather pairing + stall + NaN | Empty paired DataFrame; `exit()` blocked; 2 stalls; `ANSWER=NaN`. |
| 1 | 20 | weather pairing + stall | Paired station unresolved; `exit()` blocked; sentinel final, `ANSWER` unset. |
| 2 | 4 | stale ANSWER on unanswerable | Final sentence correctly says Kennedy Town does not exist, but the old numeric `ANSWER` (`3.274…`) remained. |
| 2 | 5 | weather pairing + verifier reset | Pre answer was an AQHI value; verifier reset to unavailable; temperature answer never recovered. |
| 2 | 8 | stale ANSWER on unanswerable | Final sentence correctly says January 2026 has no data, but `ANSWER=15.26` from January 2025 remained. |
| 2 | 14 | weather join + string in numeric ANSWER | Direct hourly join failed (`numpy.int64` binding); verifier wrote the text `"No data available"` into `ANSWER`. |
| 2 | 17 | weather pairing + stall | No wind-speed rows; `exit()` blocked; sentinel final, `ANSWER` unset. |
| 2 | 18 | handoff/aggregation + stall | `multivariate_meteo` had the correct 4.24 m/s, but synthesis reported the PM2.5 value 78.4; 2 stalls. |
| 2 | 20 | weather pairing + stall | `KeyError: station_id` in the paired frame; sentinel final, `ANSWER` unset. |
| 3 | 5 | wrong aggregation + stall | `ANSWER='January'`; 2 stalls; no numeric temperature difference. |
| 3 | 7 | wrong aggregation + verifier ambiguity | Synthesis returned the mean 16.55 instead of the highest monthly average 30.16; verifier then called Yuen Long ambiguous. |
| 3 | 13 | wrong aggregation + verifier ambiguity | Synthesis returned the mean 16.36 instead of the 95th percentile 40.0; verifier then called Tung Chung ambiguous. |
| 3 | 14 | weather join / no data | Direct hourly join and station-code mismatch; final “no data” refusal for a case with a valid answer. |
| 3 | 15 | wrong aggregation + bind failure | Synthesis returned the mean 15.26 instead of the day count 31; verifier reported the missing `station_id` bind. |
| 3 | 19 | station selection + stall | Highest-NO2 station unresolved; 1 stall; sentinel final, `ANSWER` unset. |
| 3 | 20 | weather join + NaN | `hsqa.station_code` does not exist; verifier wrote `NaN`; final “no valid entries” refusal. |

Summary of the 19 failures:

- **13/19 are weather-paired questions** (`#5`, `#14`, `#17`, `#18`, `#20`).
  Most are the same v2 prompt defect — the missing `wx_` station-code join — but
  `#18` repeat 2 is a handoff failure after the correct paired value was already
  staged. The join is still the largest single cause.
- **10/19 involved the stall guard** (34 stalls total); 8 ended with the stop
  sentinel. The guard prevents runaway loops but frequently consumes the
  producer budget without a usable value.
- **8/19 left `ANSWER` unset**, **3/19 wrote `NaN`**, **2/19 left a stale numeric
  `ANSWER` on an unanswerable premise**, **2/19 wrote a string into a numeric
  `ANSWER`** (`#14` repeat 2, `#5` repeat 3), and the other four were wrong
  numeric/aggregation answers.
- **5/19 failures had a verifier-changed `ANSWER` that still failed**. The
  verifier is not a reliable fallback for a missing or wrong staged value.
- The clearest coordination failure is `#18` repeat 2: the correct 4.24 m/s was
  already in `multivariate_meteo`, but synthesis ignored it and reported a
  PM2.5 value; the final answer was the stall sentinel.

No multi-arm failure is hidden in the aggregate: the official scorer artifact
and the saved per-case `final_text`, `last_error`, `worker_runs` and verifier
records support every row above.

## Run order and limits

The measurement order matters:

1. Day 4 10-step v2 reference (historical, v1 44/60; v2 re-score 46/60).
2. Day 4 20-step matched-budget rerun (v1 and v2 both 46/60). This replaced the
   10-step table as the single-arm Day 9 reference.
3. Day 5 preliminary multi 1-repeat (11/20 v1, 13/20 v2).
4. Day 5 3-repeat attempt that ended at `#19` without `run.json`.
5. Day 5b official multi 3-repeat (this artifact).

Hold-out is not clean generalization evidence: some hold-out failures were
inspected before the freeze. Task-level results and the paired analysis are
primary; hold-out is descriptive. Repeats are not independent samples, so the
case-run percentage is not a confidence interval. The exact task-level McNemar
test and paired task bootstrap are reported with the comparison.

The frozen v2 single arm predates the merged-role addendum. It sees the same base
prompt, but not the multi role procedures, so this comparison measures
“multi topology plus role procedures” versus “single base prompt,” not topology
alone. The v3 rerun did add the same procedural role text to the single arm
(a merged-roles addendum) and reran both arms; no v2 artifact is rewritten.

## Reproduce

```powershell
uv run python evals/run_baseline.py --arm multi --repeats 3 \
  --out runs/multi/day5b_v2_budget20_3x
uv run python evals/compare_arms.py \
  --single runs/baseline/day4_v2_budget20 \
  --multi runs/multi/day5b_v2_budget20_3x \
  --out runs/multi/day5b_v2_budget20_3x/compare.json
```

If the run is interrupted, resume with:

```powershell
uv run python evals/run_baseline.py --arm multi --repeats 3 \
  --resume runs/multi/day5b_v2_budget20_3x
```

## v3 prompt rerun

### v3 protocol

The v2 result above is frozen. v3 changed only the shared base prompt: it now
states the explicit air-quality/weather pairing rule

```sql
weather_station.station_code = 'wx_' || air_quality_station.station_code
```

and the common alias form `ws.station_code = 'wx_' || s.station_code`. It also
says that the two hourly tables are joined through their own station rows and
the two station-code namespaces, never by `station_id`. The v3 prompt is a
prose rule, not a copied truth query; the leakage tests cover the rule and the
merged-role text.

The v2 questions, truth SQL, numeric tolerances, scorer and `ANSWER` contract
are unchanged. The multi role addendum is byte-identical to v2. The single arm
uses `--single-roles`, which appends a delimited, procedure-only
`MERGED_ROLE_ADDENDUM` generated from `MULTI_RULES` and all five role prompts.
This makes the comparison **multi topology plus role procedures** versus
**single agent with the same base prompt and the same procedural text**, rather
than topology alone. The v3 single is a fairness control; the frozen v2 single
artifact remains the historical single reference.

| Item | Value |
| --- | --- |
| Single artifact | `runs/baseline/day5b_v3_budget20_3x/run.json` |
| Multi artifact | `runs/multi/day5b_v3_budget20_3x/run.json` |
| Model | `qwen2.5-coder:14b` |
| Base prompt sha256 (assembled) | `538596df6c0122fc57f5cc9fe4212ee9b92206095ae71f157f3c5288ffc0b89d` |
| Single merged-role sha256 | `2c0a50e48797d11f7b2b8d416447681eb643e482ac39cc6a0117297c941e641d` |
| Multi addendum sha256 | `141925a0538b2e039cb0d4ac78589ff0ddd3a99e3e5a27b5cd322976289c23a1` |
| Base / merged role sizes | 10,347 chars / 5,018 chars |
| Solver budget | 20 steps + 1 coordinator call |
| Producer / synthesis / verifier reserve | 11 / 3 / 6 |
| Repeats | 3, one attempt per task per repeat |
| Primary scorer | v2 (v1 and semantic stored per case) |
| Verifier mode | `overwrite` |

The base prompt hash includes the live current-time/data-window text injected by
`get_system_prompt`, so it is artifact-specific and cannot be recomputed from
the repository alone. The single run was stopped deliberately after 51 saved
case files and resumed; the multi process exited after 36 saved files and was
resumed. In each resume, only the in-flight case was rerun, so no case was run
twice for the final artifact. Both artifacts record `resumed: true`.

### v3 results

| Arm | v1 | v2 (primary) | Semantic | Repeats | Hold-out | Non-hold-out | Tokens | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Single v3 + merged roles | 41/60 (68.3%) | **45/60 (75.0%)** | 45/60 | 14/16/15 | 11/18 (61.1%) | 34/42 (81.0%) | 1,015,175 | 2,680.7 |
| Multi v3 | 38/60 (63.3%) | **40/60 (66.7%)** | 40/60 | 16/11/13 | 8/18 (44.4%) | 32/42 (76.2%) | 3,184,398 | 6,466.5 |

| Arm | Mean steps | Calls | First attempt | Tokens/case | Seconds/case | Stalls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Single v3 + merged roles | 2.70 | 162 | 52/60 | 16,920 | 44.7 | 0 |
| Multi v3 | 11.63 | 758 | 38/60 | 53,073 | 107.8 | 37 |

The multi arm is **5 case-runs lower** than the v3 single control (40 vs 45).
The task-level comparison is **2 wins, 6 losses, 12 ties** (wins `#5`, `#15`;
losses `#3`, `#4`, `#6`, `#8`, `#16`, `#17`). The exact McNemar/binomial test
gives **p=0.2891**; the paired task bootstrap is **-0.083**, 95% CI
**[-0.183, +0.017]** (5,000 task resamples, seed 20261114). The point estimate
is negative and the task-level test does not reject the null. This is again a
negative/null result, not evidence of a multi-agent gain.

The explicit join did **not** improve the aggregate result. v3 multi is one
case-run lower than v2 multi (40 vs 41); v3 single is one lower than v2 single
(45 vs 46). The v3 single is not a like-for-like replacement for the v2 single
because it now carries the merged role procedures. On the primary scorer, v3
does not repair `#14` or `#20`; `#17` is still 0/3 for multi and 2/3 for single.

Cost per case is **3.14x tokens, 2.41x wall-clock and 4.68x calls** relative to
the v3 single control. Do not read that as an efficiency improvement over the
v2 5.21x/9.11x ratios: the v3 single denominator is itself more expensive
(1.73x tokens and 3.55x seconds versus v2 single) because of the merged role
text. The multi absolute cost is roughly unchanged: 3.18M tokens and 6,466.5s
versus v2's 3.07M tokens and 6,877.5s, with 758 versus 748 calls and 37 versus
34 stalls. The planner call remains outside the 20-step solver cap, so this is
still a lower bound on an all-call budget.

### v3 category, difficulty and per-case counts

| Group | Single v3 | Multi v3 |
| --- | ---: | ---: |
| retrieval | 12/12 (100%) | 10/12 (83.3%) |
| temporal | 10/12 (83.3%) | 9/12 (75.0%) |
| spatial | 12/12 (100%) | 12/12 (100%) |
| statistics | 6/12 (50.0%) | 6/12 (50.0%) |
| multi_table | 5/12 (41.7%) | 3/12 (25.0%) |
| simple | 12/12 (100%) | 10/12 (83.3%) |
| medium | 25/30 (83.3%) | 24/30 (80.0%) |
| hard | 8/18 (44.4%) | 6/18 (33.3%) |

| # | Category | Difficulty | Hold-out | Single | Multi | Delta |
| ---: | --- | --- | :---: | ---: | ---: | ---: |
| 1 | retrieval | simple | no | 3/3 | 3/3 | +0 |
| 2 | retrieval | simple | yes | 3/3 | 3/3 | +0 |
| 3 | retrieval | medium | no | 3/3 | 2/3 | -1 |
| 4 | retrieval | simple | no | 3/3 | 2/3 | -1 |
| 5 | temporal | medium | no | 1/3 | 2/3 | +1 |
| 6 | temporal | medium | yes | 3/3 | 2/3 | -1 |
| 7 | temporal | medium | no | 3/3 | 3/3 | +0 |
| 8 | temporal | simple | no | 3/3 | 2/3 | -1 |
| 9 | spatial | medium | no | 3/3 | 3/3 | +0 |
| 10 | spatial | medium | no | 3/3 | 3/3 | +0 |
| 11 | spatial | hard | yes | 3/3 | 3/3 | +0 |
| 12 | spatial | medium | no | 3/3 | 3/3 | +0 |
| 13 | statistics | medium | no | 3/3 | 3/3 | +0 |
| 14 | statistics | hard | yes | 0/3 | 0/3 | +0 |
| 15 | statistics | medium | no | 0/3 | 1/3 | +1 |
| 16 | statistics | medium | no | 3/3 | 2/3 | -1 |
| 17 | multi_table | hard | yes | 2/3 | 0/3 | -2 |
| 18 | multi_table | hard | no | 0/3 | 0/3 | +0 |
| 19 | multi_table | hard | no | 3/3 | 3/3 | +0 |
| 20 | multi_table | hard | yes | 0/3 | 0/3 | +0 |

The multi loss is not hold-out-only: hold-out is 8/18 versus 11/18, and
non-hold-out is 32/42 versus 34/42. With this sample size the hold-out split is
descriptive, not a generalization test.

### v3 verifier audit

The verifier still does most of the work, but v3 exposes three regressions that
v2's stored flags did not:

| Repeat | Pre-verify | Final | Wrong->right | Right->wrong | Right->right | Wrong->wrong | Changed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 7/20 | 16/20 | 10 | 1 | 6 | 3 | 12 |
| 2 | 5/20 | 11/20 | 7 | 1 | 4 | 8 | 9 |
| 3 | 5/20 | 13/20 | 9 | 1 | 4 | 6 | 11 |
| **Total** | **17/60** | **40/60** | **26** | **3** | **14** | **17** | **32** |

The three right->wrong cases are real verifier harm, not just text quality:

- `#18` repeat 1: synthesis staged the correct **4.24 m/s**; `overwrite` changed
  it to **3.71 m/s**.
- `#3` repeat 2: synthesis correctly refused the no-data CO question; the
  verifier replaced that with the identical-code-block stop sentinel.
- `#16` repeat 3: synthesis correctly said no PM2.5 reading was available; the
  verifier replaced it with a station-ambiguity message.

Six of the 20 failures had a verifier-changed answer and still failed. The
verifier is not a reliable fallback for a missing or wrong staged value; its
contract needs an explicit "unset `ANSWER` on invalid premise" and "never write
a string or NaN into a numeric `ANSWER`" rule before it can be considered safe
outside this benchmark.

### v3 failure taxonomy

All **20 failing case-runs** are classified below. Overlaps are possible; the
"primary class" is the first blocker, not the only one.

| Repeat | Case | Primary class | Evidence |
| ---: | ---: | --- | --- |
| 1 | 14 | direct AQ/weather id join + empty-frame format | Last SQL joins `a.station_id = w.station_id`; empty result; `f"{None:.2f}"` raises `TypeError`; final false no-data. |
| 1 | 17 | stall guard + `exit()` | Station guard calls `exit()`; blocked by the sandbox; 2 stalls; stop sentinel and no answer. |
| 1 | 18 | verifier overwrite harm | Pre-verifier value 4.24 is correct; verifier writes 3.71; follow-up SQL also has a syntax error. |
| 1 | 20 | hallucinated table + stall | Queries nonexistent `station_type_definitions`; 1 stall; final says there are no wind-below-2 rows. |
| 2 | 3 | verifier overwrite harm + stall | Pre-verifier refusal is correct; `exit()` is blocked; verifier writes the stop sentinel. |
| 2 | 4 | empty AVG + `None` format + stall | Null average formatted with `:.2f` raises `TypeError`; `exit()` blocked; stop sentinel; expected invalid-station refusal. |
| 2 | 5 | stall + wrong temperature path | Weather query uses `AVG(datetime)` instead of temperature; `exit()` blocked; sentinel; answer 3.24 versus 12.40. |
| 2 | 8 | bind error + stale value + stall | Missing `station_id` bind; answer 15.25 for January 2026 (expected no-data/0); 1 stall. |
| 2 | 14 | direct AQ/weather id join + stall | Same direct `station_id` join; `exit()` blocked; 1 stall; false no-data and stale 3.28. |
| 2 | 15 | wrong grain (hourly count) | Returns 81 instead of 31; counts hourly exceedances rather than daily-average days; follow-up hits a `numpy.int64` bind error. |
| 2 | 17 | station guard + `exit()` | Tai Po guard reports "not uniquely identified"; `exit()` blocked; 1 stall; no answer. |
| 2 | 18 | stall + handoff/aggregation | 2 stalls; answer 15.26; stop sentinel; the correct paired wind path is not handed off. |
| 2 | 20 | shared-timestamp filter + wrong arithmetic | Returns 41.37 versus 31.18; filters any weather station with wind below 2, not each station's paired row; final "cannot be answered". |
| 3 | 6 | station guard + stale AQHI answer | Tung Chung guard fails; stale AQHI count list remains; missing `station_id` bind; expected 0.28. |
| 3 | 14 | `numpy.int64` bind + false no-data | Station is resolved, but the id is a `numpy.int64`; final says no data for Causeway Bay. |
| 3 | 15 | bind error + stale mean | Missing `station_id`; answer 15.26; final tells the user to fix the bind; expected 31 days. |
| 3 | 16 | verifier overwrite harm | Pre-verifier no-data sentence is correct; verifier replaces it with a station-ambiguity sentence. |
| 3 | 17 | `numpy.int64` bind + stall | Bind error; 1 stall; answer 2.95 versus 1.81; stop sentinel. |
| 3 | 18 | stall + handoff/aggregation | `exit()` blocked; 2 stalls; answer 15.26; stop sentinel; expected 4.24. |
| 3 | 20 | shared-timestamp filter | CTE filters every `wx_%` weather row with wind below 2; returns 32.43 versus 31.18; verifier writes the numeric. |

Summary of the 20 v3 failures:

- **13/20 involve weather pairing or multi-table weather questions** (`#5`,
  `#14`, `#17`, `#18`, `#20`). The explicit prompt rule did not remove the
  failure: `#14` still uses the direct `station_id` join in repeats 1 and 2,
  and `#20` repeat 3 filters by any weather station instead of the paired one.
  A prose rule is not a substitute for verifier checks or a stronger model.
- **16/20 had at least one stall** (37 stall events total); **8** ended in the
  identical-code stop sentinel. The guard saves budget but often replaces a
  wrong attempt with no answer.
- **7/20 left `ANSWER` unset**, **4/20 wrote a string or NaN into `ANSWER`**,
  and the other 9 were wrong numeric/aggregation answers. The verifier's
  `overwrite` rescue rate (26) is offset by three right->wrong rewrites.
- The most damaging new evidence is `#18` repeat 1: the correct 4.24 m/s was
  already staged, but the verifier overwrote it with 3.71 m/s. This is the
  failure mode the verifier-mode comparison must attack.

### v3 reproduce and limits

```powershell
uv run python evals/run_baseline.py --arm single --single-roles --repeats 3 \
  --out runs/baseline/day5b_v3_budget20_3x
uv run python evals/run_baseline.py --arm multi --repeats 3 \
  --out runs/multi/day5b_v3_budget20_3x
uv run python evals/compare_arms.py \
  --single runs/baseline/day5b_v3_budget20_3x \
  --multi runs/multi/day5b_v3_budget20_3x \
  --scorer v2 --out runs/multi/day5b_v3_budget20_3x/compare.json
```

Resume commands if a process is killed:

```powershell
uv run python evals/run_baseline.py --arm single --single-roles --repeats 3 \
  --resume runs/baseline/day5b_v3_budget20_3x
uv run python evals/run_baseline.py --arm multi --repeats 3 \
  --resume runs/multi/day5b_v3_budget20_3x
```

Limits: the v3 base prompt hash is live-context-specific; the single arm is a
merged-role control, not the frozen reference; the result is a negative/null
result with 20 tasks and 3 repeats; hold-out is descriptive; and the planner
call is outside the 20-step solver budget. No v3 prompt tuning was performed
after the result was seen. The next controlled comparison is verifier
`flag`/`none` versus `overwrite` on the non-hold-out cases; the default remains
`overwrite` until that comparison is recorded.
