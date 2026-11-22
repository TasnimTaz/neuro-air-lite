# v5 DEV diagnosis

Context: Windows host, repository `neuro-air-lite`, no DB and no model call in this step.
The supplied `evals/diagnose_v5.py` and `tests/test_v5_diagnose.py` were copied unchanged from
`C:\Users\Optimus_Prime\Desktop` into the repo. Their SHA256 hashes after copying:

```
diagnose_v5.py      DD6C4F19171667E575F4E26E718898059149D4A81753027DEDABA4F1FA20E91C
test_v5_diagnose.py 1A110062A995EC9FDC581CD0DD5050EECFEC1B744238CE9FD43B1195E0731BB1
```

The requested paths `runs/v5/dev_single` and `runs/v5/dev_multi` did not exist; the actual qwen
DEV folders did. Exact copies were made under the requested names without editing any case:

```
run_json_sha_single=8DF2EB73AE7850EEBED45C01F2A1879A82039484AB1D38C3B1EF841FE939F03E
run_json_sha_multi=18E79D304B9ACD6C81F554614E49B49B8BB78499C2892C7B9EDF470C1A43EA48
cases: single=53, multi=53
```

No field-name adaptation was needed and no repeated `task_id` was found. The script ran on the
supplied schema as-is.

## Test command and output

```
uv run pytest -q tests/test_v5_diagnose.py
......                                                                   [100%]
6 passed in 0.14s
```

## Diagnosis command and full output

```
uv run python evals/diagnose_v5.py runs/v5/dev_single runs/v5/dev_multi
```

```
[schema] single: 53 scored cases; keys of first case: ['arm', 'budget_exhausted', 'calls', 'category', 'difficulty', 'executions', 'family', 'final_text', 'first_attempt_ok', 'infra_failed', 'infra_retries', 'last_error', 'pass', 'seconds', 'split', 'steps', 'task_id', 'tokens', 'validator']
[schema] single: optional fields absent (related sections degrade): ['pass_pre_verify', 'stall_count', 'worker_runs', 'verifier_changed', 'verifier_rejections']
[schema] multi: 53 scored cases; keys of first case: ['arm', 'budget_exhausted', 'calls', 'category', 'difficulty', 'executions', 'family', 'final_text', 'first_attempt_ok', 'infra_failed', 'infra_retries', 'last_error', 'pass', 'pass_pre_verify', 'plan', 'plan_source', 'planner_tokens', 'seconds', 'split', 'stall_count', 'steps', 'task_id', 'tokens', 'validator', 'verifier_changed', 'verifier_mode', 'verifier_rejections', 'worker_runs']

== 1. Headroom (n=53)
single 49/53 (92%); multi 42/53 (79%)
multi can win at most 4 (single failures) and lose up to 49 (single passes); observed wins=2 losses=9
[ceiling] single > 90%: this set cannot show a multi-agent gain whatever multi does; treat as uninformative for the gain question

== 2. Why multi lost
    7  wrong value before verifier (upstream/synthesis)
    2  required variable never staged
   task  26 [comparative/hard] cause=wrong value before verifier (upstream/synthesis) stalls=0 steps=11 verifier_changed=True bad=['roadside_mean:|20.509388335704106-20.26|>0.1013', 'general_mean:|15.428776908968082-15.26|>0.0763', 'roadside_minus_general:|5.080611426736024-5|>0.025']
   task  31 [multitable/medium] cause=wrong value before verifier (upstream/synthesis) stalls=1 steps=15 verifier_changed=False bad=['mean_wind_speed:|2.6829187675070023-4|>0.02', 'mean_temperature:|22.95888278388275-27.65|>0.13825']
        last_error: 'ERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite\\core\\runtime_v5.py", line 123, in e'
   task  87 [multitable/hard] cause=wrong value before verifier (upstream/synthesis) stalls=1 steps=15 verifier_changed=True bad=['mean_pm25_calm:non-finite value', 'calm_hours:0 != 123']
        last_error: 'Matched station: Mong Kok (mongkok)\n\nERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite\\core\\runtime_v5.py", line 123, in e'
   task 105 [comparative/hard] cause=wrong value before verifier (upstream/synthesis) stalls=0 steps=13 verifier_changed=True bad=['roadside_mean:|63.849839400428266-64.3|>0.3215', 'general_mean:|32.85631573356007-33.27|>0.16635']
   task 106 [multitable/hard] cause=wrong value before verifier (upstream/synthesis) stalls=1 steps=16 verifier_changed=False bad=['mean_pm25_calm:|11.696969696969695-11.77|>0.05885', 'calm_hours:99 != 603']
        last_error: 'Matched station: Southern [south]\n\nERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite\\core\\runtime_v5.py", line 123, in e'
   task 174 [multitable/hard] cause=wrong value before verifier (upstream/synthesis) stalls=1 steps=15 verifier_changed=False bad=['calm_hours:422 != 417']
        last_error: 'Matched station: Tai Po [taipo]\n\nERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite\\core\\runtime_v5.py", line 123, in e'
   task 179 [statistics/medium] cause=wrong value before verifier (upstream/synthesis) stalls=1 steps=8 verifier_changed=True bad=['p95_pm2_5:non-finite value']
        last_error: 'Resolved station: Eastern [east]\n\nERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite\\core\\runtime_v5.py", line 123, in e'
   task 210 [temporal/hard] cause=required variable never staged stalls=1 steps=8 verifier_changed=False bad=['peak_hour:variable not set', 'peak_hour_mean:variable not set']
        last_error: 'ERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite\\core\\runtime_v5.py", line 123, in e'
   task 241 [multitable/hard] cause=required variable never staged stalls=0 steps=14 verifier_changed=False bad=['mean_pm25_calm:variable not set', 'calm_hours:variable not set']
        last_error: 'Resolved station: Tap Mun (ID: 33)\n\nERROR:\nTraceback (most recent call last):\n  File "C:\\Users\\Optimus_Prime\\Desktop\\Multi-Agent-Research_Project\\neuro-air-lite'

== 3. Stalls (an ASSOCIATION; a hard task can cause both stalls and failure)
  multi pass with >=1 stall: 13/21 (62%)   without: 29/32 (91%)
  CONTROL: single pass on the same stalled tasks: 18/21 (86%)
  Read it this way: if single ALSO fails the stalled tasks, stalls mark hard tasks. If single passes them and multi fails, stalls point at the multi harness.
  stalls by role: {'verification': 18, 'multivariate_meteo': 1, 'data_acquisition': 1, 'synthesis_report': 2}
  stalled tasks that ended with a required variable unset: 2

== 4. Topology vs verifier
  single 49/53 (92%) | multi pre-verifier 27/53 (51%) | multi final 42/53 (79%)
  verifier rescued 15, broke 0; safe-mode rejections: 2

== 5. Accuracy by category / difficulty (single | multi)
  category   comparative  n=11  single 11/11  multi 9/11
  category   multitable   n=8   single 7/8  multi 2/8
  category   retrieval    n=11  single 11/11  multi 11/11
  category   spatial      n=10  single 9/10  multi 9/10
  category   statistics   n=9   single 8/9  multi 8/9
  category   temporal     n=4  single 3/4  multi 3/4
  difficulty hard         n=21  single 17/21  multi 12/21
  difficulty medium       n=28  single 28/28  multi 26/28
  difficulty simple       n=4   single 4/4  multi 4/4
```

## Plain-language reading

The DEV set is already near a ceiling for single-agent performance: single passed 92%, leaving
only four tasks on which multi could possibly show a gain. Multi won two and lost nine; seven
losses had a wrong value before the verifier and two never staged a required variable. The
verifier rescued 15 tasks and broke none, so the final multi score is much better than its
51% pre-verifier score, but it still ends below single. Stalls are associated with lower multi
pass rates, yet single passes 86% of the same stalled tasks, so stalls are not simply a label
for inherently hard questions. This DEV diagnosis cannot show a multi-agent gain at this
ceiling; it is evidence about failure modes only, and no TEST result was inspected.
