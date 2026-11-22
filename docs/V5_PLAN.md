# v5: replicating the paper's single-vs-multi ablation

v4 stays frozen as the honest negative result (multi 39/60 vs single 47/60, 20 tasks).
v5 changes the *measurement*, not the prompts you tuned on v4.

## What changed and why

| Piece | File | Why (evidence in your own logs) |
| --- | --- | --- |
| Clean `exit()`, numpy binds | `core/runtime_v5.py` | 8/19 (v2) and 8/20 (v3) multi failures ended on the stall sentinel after a blocked `exit()`; `numpy.int64` bind errors in v3 #14/#15/#17 |
| Named output variables | `evals/v5/tasks.py`, `domains/hk/roles_v5.py` | Paper validators read stored variables, not prose; removes stale/NaN/string `ANSWER` and sentinel-text failures that only exist in the multi arm |
| `verifier_mode="safe"` | `core/verifier_safe.py`, `core/orchestrator.py` | Verifier wrote NaN/strings and replaced correct refusals with the stall sentinel; `overwrite` still available |
| 100+ stratified tasks, 1 attempt | `evals/v5/tasks.py` | Simulated power at the paper's effect size: 20 tasks ~0%, 90 ~36%, 180 ~73% (exact McNemar) |
| Runner / comparison | `evals/run_v5.py`, `evals/compare_v5.py` | Paper protocol: infra failures re-run not scored, paired McNemar, task-cluster bootstrap |

Frozen v2-v4 files are untouched. `core/orchestrator.py` gained `verifier_mode="safe"` and
`answer_vars=("ANSWER",)`; defaults reproduce the old behaviour (existing orchestrator tests pass).

## Run order (do not skip the gates)

```powershell
# 0. tests
uv run pytest -q tests/test_v5_*.py

# 1. generate + freeze the benchmark (needs the DB; recomputes every truth value)
uv run python -m evals.v5.tasks generate --n 260 --seed 20261122 --out evals/v5/tasks_v5.json
uv run python -m evals.v5.tasks check evals/v5/tasks_v5.json
git add evals/v5/tasks_v5.json; git commit -m "Freeze v5 tasks"      # never edit afterwards

# 2. GATE A - harness, on the DEV split only (about 20% of tasks)
uv run python evals/run_v5.py --split dev --arm single --out runs/v5/dev_single
uv run python evals/run_v5.py --split dev --arm multi  --out runs/v5/dev_multi
#    Proceed only if first_attempt_success >= 0.95 in BOTH arms and multi stalls ~ 0.
#    If not, fix the harness (never the test split, never the task file) and rerun dev.

## Gate A amendment (2026-11-22, AFTER seeing the DEV result)

The original rule above is kept verbatim for audit. It is amended to an **INTEGRITY gate only**:

* frozen files unchanged since freeze commit `72d0ec8` (Step 0 output);
* scorer unchanged;
* infra failures (`steps == 0 AND tokens == 0`, or the harness's `infra_failed` flag) are excluded
  **symmetrically** in both arms and re-run until both arms have the same number of valid tasks;
* the TEST split is never inspected before the TEST run finishes.

Stall count is now a **reported outcome**, not a gate condition. The reason for the amendment is
that `multi stalls ~ 0` is a performance condition, not an integrity condition, and as written it
blocks the only unbiased TEST run indefinitely. First-attempt success remains a reported DEV
diagnostic.

Pre-declared before any TEST run: if single's TEST pass rate is above 90%, the TEST split is
uninformative for the GAIN question (it can still show harm) and will be reported that way.
Secondary, also pre-declared: results on the 'hard' difficulty tier, defined by task type in
`evals/v5/tasks.py` and never by which tasks single failed.

# 3. headline runs on the TEST split, for each backbone (>= 2, ideally 3)
uv run python evals/run_v5.py --split test --arm single --model <m> --out runs/v5/<m>_single
uv run python evals/run_v5.py --split test --arm multi  --model <m> --out runs/v5/<m>_multi --verifier-mode safe

# 4. paired analysis
uv run python evals/compare_v5.py --pair runs/v5/<m1>_single runs/v5/<m1>_multi --pair runs/v5/<m2>_single runs/v5/<m2>_multi
```

Secondary, pre-declared: rerun multi with `--verifier-mode overwrite` and `flag` on the same
test split and report all three; the headline uses `safe`.

## "Nearly same as the paper" means

* pooled multi >= single over >= 2 backbones, task-cluster bootstrap CI mostly above 0;
* per-backbone gains roughly +3 to +10 pp (paper: +3.3 to +10.0);
* multi uses about 3-4x the median tokens (paper: 3.3-4.4x);
* first-attempt execution success >= 95% in both arms (paper: 99.3%).

Even a faithful replica will often be non-significant per backbone (paper: p = .031, .031,
.078, .125, .453). Judge the pooled estimate.

## Not verified by the author of this pack

* Truth SQL is syntax-checked with the Postgres parser (pglast) but was never executed on your
  database. `generate` is the real test; it prints every dropped template with the reason.
* No real model was run. The end-to-end test uses a scripted fake LLM.
* numpy adapters were tested with `psycopg2.extensions.adapt`, not a live connection.
* `core/subprocess_runtime.py` (API path) is not patched; call `register_numpy_adapters()` in the
  child too if you want the same behaviour there.
* `--extra-body` (switching off provider thinking) is provider-specific; check the provider docs.
  DeepSeek thinking-mode runs are not comparable with non-thinking local runs.
* The multi arm's hand-off in the paper's own adapter (`multiagent_adapter.py`) was not available,
  so worker-to-worker context passing here is still your v4 design (manifest + shared runtime).
