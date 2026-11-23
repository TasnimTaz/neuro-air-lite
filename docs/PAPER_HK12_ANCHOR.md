# External HK-12 anchor: paper verification, adapter, and honest result

This is the Step 4 external anchor. It does **not** replace the frozen v5 TEST
result in `docs/V5_TEST_REPORT.md`, and it does **not** reproduce the paper's
five-backbone headline. Only the local `qwen2.5-coder:14b` model was used. The
`*_STRONG` keys are set in `.env` but were neither read as credentials nor used
for any call.

## 1. What was verified from the public paper repository

- `https://api.github.com/repos/acodercat/Neuro-Air` returned a public repository,
  license `MIT`, default branch `master`.
- Source clone command:
  `git clone --depth 1 https://github.com/acodercat/Neuro-Air.git <temp>/neuroair_paper`
- Source HEAD: `dbc47f0c9e2192d204a04e848b253953eb1afd72`.
- `benchmark/evals/neuroair/**/HK/` contains **12 task JSON files** and
  **15 validator `.py` files** (including the three package `__init__.py` files).
- `benchmark/experiments/Neuro-Air/pv_matrix.json` is present and was copied
  unchanged. Its SHA256 is
  `70ac70dd5cb567a0dc1dd835ddf2a32780fda76dd5f95ccbd9d1a7306d505952`.
- The HK-12 subset has 12 tasks × 5 backbones × 2 arms = 120 cells. Observed:
  **119/120 pass**. The only failing cell is
  `hk_1_10 / qwen-2.5-72b / single = F` (its multi cell is `P`).
  Equivalently: paper single **59/60**, paper multi **60/60** on these 12 tasks.

The vendored tree is `evals/vendor/neuroair_hk12/`. Its `README.md` records the
source commit and SHA256 of every copied file; `LICENSE` is the unmodified MIT
license from the source root. A unit test verifies all 29 non-README hashes.

## 2. Adapter design and frozen boundaries

`evals/paper_hk12.py` runs each original HK query plus its original
`requirements` through this project's harness. Both arms use the same roles,
runtime, base prompt, temperature `0.2`, `max_steps=20`, and
`verifier_mode=safe`. The base system-prompt hash was
`ff04f301a5eebdc041b8e0e58260ab5cd4011729463f94c785e2201dacea585f`, the same
one used in the v4 runs.

No file under `core/`, `domains/hk/`, `evals/v5/`, or `evals/run_v5.py` was
edited. No prompt, role, scorer, question, or tolerance was edited. The vendored
paper validators are byte-for-byte copies; they are made importable by a small
runtime shim that supplies:

- `cave_agent.runtime.IPythonRuntime` / `Variable`,
- `domains.HK.db.engine` (the read-only engine),
- `core.types.Turn`,
- `core.validation` (`ValidatorResult`, `is_finite_number`, `compare_numeric`),
- `runtime.retrieve(var)` mapped to this project's runtime namespace.

The paper schema numbers the same stations `1..18`; the lite DB uses `19..36`.
The map was taken from the paper's own `benchmark/domains/HK/prompts.py`. It is
applied only to paper ground-truth SQL (`station_id = N`) and to the one output
variable `higher_peak_station_id` declared in the paper requirements as `16 or
5`. Validator source, questions, roles, and scorer remain unchanged.

Validator smoke check:

```
uv run python evals/paper_hk12.py --smoke
...
[smoke] tasks=12 unexpected=0
```

All 12 validators imported and their SQL executed on the local read-only DB.

## 3. Run executed, and the time-cap skip

Single arm, completed:

```
uv run python evals/paper_hk12.py --arm single --repeats 3 \
  --out runs/paper_hk12/qwen14b_single --model qwen2.5-coder:14b \
  --base-url http://127.0.0.1:11434/v1
```

Artifact evidence: 36 case files, `run.json`, `infra_retries_total=0`, no
watchdog abort, `runner_exit=0`, `watcher_exit=0`. Multi arm was started with the
same settings plus `--verifier-mode safe`, but the full 12×3 multi arm did not
finish within the pre-agreed ~2 hour Step 4 cap. The orchestrator status was
stopped at **10/36 multi case files** and no multi `run.json` was produced.

Therefore the HK-12 **paired single-vs-multi comparison is skipped**, and the
partial multi files are not used for a gain, loss, significance, or ceiling
claim. The single-arm scores below are complete; the local multi column is
marked `not completed`, not `0`.

## 4. Local single arm vs paper matrix

Local single: **11/36 (30.6%)**, mean 36,483 tokens/case, mean 94.4 s/case,
zero infra retries. Each task had 3 repeats.

| Task | Local single (3 repeats) | Local multi | Paper single (5 backbones) | Paper multi (5 backbones) | Paper failing cells |
|---|---:|---|---:|---:|---|
| hk_1_1 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_1_10 | 0/3 | not completed | 4/5 | 5/5 | `qwen-2.5-72b/single` |
| hk_1_11 | 3/3 | not completed | 5/5 | 5/5 | none |
| hk_1_13 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_1_2 | 3/3 | not completed | 5/5 | 5/5 | none |
| hk_1_5 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_1_6 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_1_7 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_1_9 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_2_18 | 0/3 | not completed | 5/5 | 5/5 | none |
| hk_2_6 | 3/3 | not completed | 5/5 | 5/5 | none |
| hk_3_11 | 2/3 | not completed | 5/5 | 5/5 | none |

Paper ceiling on these 12 tasks: single 59/60, multi 60/60, 119/120 cells. This
external anchor is therefore already at ceiling for the paper line-up and
cannot demonstrate gain; it can only expose failures such as the one recorded
`qwen-2.5-72b/single` miss.

## 5. Honest interpretation and limitations

- The local 14B single arm is far below the paper line-up (30.6% vs paper
  98.3% single on these cells). This is **not** a replication of the paper's
  HK result and is not evidence for or against multi-agent gain.
- A concrete adapter limitation is visible in the case files: the original
  queries use paper station IDs (for example 16 and 5), while our base prompt
  lists local IDs (35 and 28). The validator side is normalized, but the model
  still sees its original question and our unchanged base prompt, so several
  tasks answer “data not available”. Adding a crosswalk to the prompt would be
  a question/prompt edit and was deliberately not done.
- Some tasks hit the 20-step budget (for example `hk_1_6`, 223,032 tokens,
  `[STEP_BUDGET_EXHAUSTED]`). These are reported outcomes, not infra failures;
  infra retries were zero.
- No paid API, web model, expert panel, human evaluation, or strong-model run
  was used. No strong key value was printed.

## 6. Reproduction

```powershell
uv run pytest -q tests/test_paper_hk12.py tests/test_paper_vendor.py
uv run python evals/paper_hk12.py --smoke
uv run python evals/paper_hk12.py --arm single --repeats 3 --out runs/paper_hk12/qwen14b_single --model qwen2.5-coder:14b --base-url http://127.0.0.1:11434/v1
uv run python evals/paper_hk12.py --arm multi  --repeats 3 --out runs/paper_hk12/qwen14b_multi  --model qwen2.5-coder:14b --base-url http://127.0.0.1:11434/v1 --verifier-mode safe
```

The last command was intentionally stopped by the time cap in this session; it
is a valid reproduction command on a machine with more time.
