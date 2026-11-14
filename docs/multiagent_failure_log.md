# Day 5b: multi-agent measurement, verifier audit and failures

This is the official Day 5b multi-agent record. It supersedes the 1-repeat Day 5
signal in `docs/failure_log.md`. The single-arm reference is the matched 20-step
v2 run in `runs/baseline/day4_v2_budget20/run.json`; both arms use the same 20
questions, truth SQL, model and base prompt.

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
alone. The v3 rerun should add the same procedural role text to the single arm
(a merged-roles addendum) and rerun both arms; no v2 artifact is rewritten.

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
