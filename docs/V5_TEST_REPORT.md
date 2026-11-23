# v5 TEST split: final paired result

This is the frozen v5 TEST-split result for the local model. It is a completed
TEST run, not a DEV tuning result. No prompt, role, scorer, benchmark task,
question or split definition was changed for this run.

## Evidence in the repository

The raw `runs/` tree is gitignored and is not staged. To make the result
checkable without the raw folder:

- `docs/results/v5_test_cases.csv` — 398 generated rows (199 single + 199
  multi) with task id, arm, category, difficulty, pass, `pass_pre_verify`,
  tokens, seconds, `stall_count`, `verifier_changed` and `infra_failed`.
- `docs/results/v5_dev_cases.csv` — the same 106-row export for DEV.
- `scripts/export_case_csv.py` generates both from case JSON; it is not
  hand-edited.
- `tests/test_evidence_csv.py` re-derives 182/199, 166/199, 11 wins, 27 losses,
  90 pre-verifier passes, 77 rescued and 1 broken from the TEST CSV.
- `docs/EVIDENCE_MANIFEST.md` records the outside-repo zip path, its SHA256 and
  the SHA256 of every `run.json`. The zip itself is deliberately not staged.

## Execution and integrity

- Runner: `evals/run_v5.py`, frozen at commit `72d0ec8`.
- Benchmark: `evals/v5/tasks_v5.json`, `split=test`, 199 tasks.
- Model/endpoint: `qwen2.5-coder:14b`, `http://127.0.0.1:11434/v1` (local Ollama).
- Protocol: temperature `0.2`, `max_steps=20`, `verifier_mode=safe`, identical
  base prompt / single addendum / multi prompts to the DEV run.
- Single arm: `runs/v5/qwen14b_test_single/`, 199 case files and `run.json`,
  unique task IDs 199, `infra_failed_task_ids=[]`.
- Multi arm: `runs/v5/qwen14b_test_multi/`, 199 case files and `run.json`,
  `infra_failed_task_ids=[]`.
- No watchdog `ABORT` was emitted in either arm. DB and Ollama health checks
  passed; no DB reset or ingest was performed.
- The original single-arm wrapper disappeared at 197/199 cases with no
  `run.json`. Because there was no watchdog abort and the services were healthy,
  the same `--out` was resumed for missing task IDs `250,252`; this is the
  runner's built-in resume mechanism, not a second TEST run. It produced the
  199-case single artifact above.
- Both later wrapper `ABORT` statuses were PowerShell quoting/inspection bugs,
  not evaluation or watchdog aborts. Multi had not started before the first
  false abort and was started once; the second false abort happened only after
  the completed multi artifact was already on disk. No completed TEST arm was
  re-run to change a result.

## Paired result

`uv run python evals/compare_v5.py --pair runs/v5/qwen14b_test_single runs/v5/qwen14b_test_multi`

| Metric | Single | Multi | Difference |
|---|---:|---:|---:|
| Passed / total | 182/199 (91.5%) | 166/199 (83.4%) | −8.0 pp |
| Wins (multi right, single wrong) | — | — | 11 |
| Losses (single right, multi wrong) | — | — | 27 |
| Ties | — | — | 161 |
| Exact McNemar p | — | — | `0.0139` |
| Paired bootstrap 95% CI (pp) | — | — | `[−14.1, −2.0]` |
| Median token ratio (multi/single, per task) | — | — | `4.89` |
| Median case wall-clock ratio | — | — | `6.11` |
| Total wall-clock ratio (sum seconds) | 3,763.1 s | 20,158.8 s | `5.36×` |
| First-attempt success | 178/199 (89.5%) | 176/199 (88.4%) | −1.1 pp |
| Stall events | — | 68 (59 cases) | — |
| Verifier rejections | — | 6 | — |
| Pre-verifier pass | — | 90/199 (45.2%) | — |
| Verifier rescued / broke | — | 77 / 1 | — |

The multi-arm pre-verifier score (`90/199`) is shown next to the single final
score (`182/199`) and the multi final score (`166/199`). The verifier rescued 77
tasks and broke 1; it did not make the multi arm competitive with single.

## Categories (pass/total)

| Category | Single | Multi |
|---|---:|---:|
| comparative | 29/31 (93.5%) | 29/31 (93.5%) |
| multitable | 22/26 (84.6%) | 15/26 (57.7%) |
| retrieval | 36/36 (100.0%) | 35/36 (97.2%) |
| spatial | 43/46 (93.5%) | 36/46 (78.3%) |
| statistics | 37/38 (97.4%) | 33/38 (86.8%) |
| temporal | 15/22 (68.2%) | 18/22 (81.8%) |

## Difficulty and pre-declared hard tier

The hard tier is the task-type field already defined in `evals/v5/tasks.py`; it
was not defined post hoc from tasks the single arm failed.

| Difficulty | Single | Multi |
|---|---:|---:|
| hard | 62/73 (84.9%) | 53/73 (72.6%) |
| medium | 101/107 (94.4%) | 95/107 (88.8%) |
| simple | 19/19 (100.0%) | 18/19 (94.7%) |

## Ceiling wording

The pre-declared rule says that if the single arm exceeds 90%, TEST is
uninformative for demonstrating gain because there is little headroom, but it
can still demonstrate harm. Single scored 91.5%, so the rule applies. The
observed effect is significant harm (`−8.0 pp`, exact `p=0.0139`, bootstrap CI
entirely negative), not evidence for a multi-agent gain. This test cannot tell
us what would happen on a harder benchmark with more single-arm headroom.

## Relation to the paper

The paper reports `82.0% → 88.4%` (`+6.4 pp`, `p=2.5×10⁻⁵`) on its 90-task
benchmark. That result is not reproduced here: the paper line-up and full 90
original tasks were not run, and only one local 14B backbone was available. The
lite topology is similar, but the benchmark and result are not the same. The
v4 result was an honest negative pre-fix record (`47/60` single, `39/60` multi,
`−13.3 pp`, `p=0.3438`); v5 DEV was an exploratory post-fix negative
(`49/53` single, `42/53` multi, `−13.2 pp`, `p=0.0654`). This TEST run confirms
the negative direction with larger sample size and a significant paired test.
