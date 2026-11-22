# v5 local status (this machine)

Context: Windows host (`win32`), repository `neuro-air-lite`, Python via `uv`,
Ollama `0.40.17`. Commands below were run on 2026-11-22. No paid model call was made.

## Pack application and regression checks

The pack `C:\Users\Optimus_Prime\Desktop\neuro-air-v5-pack.zip` was applied over the clean
working tree at commit `80254d2`.

```
Get-FileHash 'C:\Users\Optimus_Prime\Desktop\neuro-air-v5-pack.zip' -Algorithm SHA256
1A1FAD0D0F06CF951B32F911B9B4AEDAF3FB704169B2CB6E1FEFA3D7640C5F1E

Get-FileHash 'evals\v5\tasks_v5.json' -Algorithm SHA256
F15BD3A2ECF093EE814C9CB2FE1393F265613CD419FAB51F8BE96BFE0A9D8169
```

The four frozen v2–v4 probes had identical SHA256 before and after extraction:

```
runtime.py        6918E708BC92C7FEDE0BB587045AC18FEF81E322CADE3133D3697FC2069B0BF4
roles.py          C299DE12D1D17AF6E1B250549380A6F83588069735E1FA8BA1AAE27BAFD9CE40
hk_baseline_v2.py 8751BF7CFB3D7A8D65A108E620E818ACD0AA838AEA5EC2DACBFFBBD241988A88
run_baseline.py   909E922FFE26DCF2DB75B8F729752A2C83DF98B32CEF976FECD236F3BB795172
```

Baseline before change, Windows host:

```
uv run pytest -q tests/test_runtime.py tests/test_orchestrator.py
66 passed in 1.44s

uv run pytest -q
246 passed, 1 skipped in 26.16s
```

`pglast` was added as a dev dependency so the pack's Postgres syntax test actually runs.
After applying the pack, Windows host:

```
uv run pytest -q tests/test_v5_e2e_fake.py tests/test_v5_runtime.py tests/test_v5_tasks.py tests/test_v5_verifier.py
56 passed in 2.27s

uv run pytest -q
302 passed, 1 skipped in 37.11s
```

PowerShell does not expand `tests/test_v5_*.py` for pytest; the four test files were listed
explicitly.

## Frozen benchmark

Command:

```
uv run python -m evals.v5.tasks generate --n 260 --seed 20261122 --out evals/v5/tasks_v5.json
```

Output:

```
candidates=258 kept=252 dropped=6 sha256=af4b7e3ec9aca2a2b0850e4df3d09cc965eff30a6104c114ff47b9818fde1d5d
by_category: {'comparative': 42, 'multitable': 34, 'retrieval': 47, 'spatial': 56, 'statistics': 47, 'temporal': 26} by_split: {'dev': 53, 'test': 199}
```

The six drops are `spatial_within_stations` tasks whose integer truths were all zero
(guessable). Verification:

```
uv run python -m evals.v5.tasks check evals/v5/tasks_v5.json
ok: 252 tasks, sha256 verified; categories={...}
```

## Backbones actually available

Environment keys were inspected without printing values. Configured values:

```
LLM_MODEL=qwen2.5-coder:14b
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL_STRONG=deepseek-flash
LLM_BASE_URL_STRONG=https://api.deepseek.com
```

`ollama list` on this Windows host showed:

```
nomic-embed-text:latest   0a109f422b47   274 MB
qwen2.5-coder:14b         9ec8897f747e   9.0 GB
llama3.1:latest           46e0c10c039e   4.9 GB
```

Local OpenAI-compatible smoke calls returned `OK` for both `qwen2.5-coder:14b` and
`llama3.1:latest`. `nomic-embed-text` is an embedding model, not a chat backbone.
`deepseek-flash` remains blocked by the previously observed `402 Insufficient Balance`,
so no paid call was attempted.

Therefore: two local chat backbones can be run, but neither is from the paper's five-backbone
lineup. Strict paper-lineup pooling is currently zero models.

## Dev gate, qwen2.5-coder:14b

Single-arm dev run, Windows host:

```
uv run python evals/run_v5.py --split dev --arm single --out runs/v5/qwen14b_dev_single --model qwen2.5-coder:14b --base-url http://127.0.0.1:11434/v1
```

Summary:

```
n=53, pass=49, accuracy=0.9245, first_attempt_success=0.9623, infra_retries_total=0
by_category: comparative 11/11, multitable 7/8, retrieval 11/11, spatial 9/10, statistics 8/9, temporal 3/4
```

The four single failures have empty `last_error`; they are answer/validator failures, not
provider or harness failures.

Both dev-run metadata files record the same base system prompt hash and the same task file.
From `runs/v5/qwen14b_dev_{single,multi}/run_meta.json`:

```
base_prompt_sha256  ff04f301a5eebdc041b8e0e58260ab5cd4011729463f94c785e2201dacea585f
single_addendum_sha256 0392d85ea259b51f76ef7616e1b53502b7a89cea4e7081894fa59d8cdc003475
multi_prompts_sha256   9abd475abb3abebc72add25cde4950a5916926c02184610bdae28b13893a3cd0
tasks_file  evals/v5/tasks_v5.json
```

The multi-arm dev run in `runs/v5/qwen14b_dev_multi` stopped at **24/53 cases** with no
`run.json`. A foreground resume attempt failed at prompt-hash setup:

```
psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432 failed:
Connection refused (0x0000274D/10061)
```

`docker compose ps` then failed because the Docker daemon was down:

```
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine;
check if the path is correct and if the daemon is running
```

No DB reset was attempted. The multi dev arm is therefore **incomplete**, and no paired
final dev comparison was produced.

### Gate A assessment (partial multi run, 23/53 cases)

`docs/V5_PLAN.md:30-34` requires both arms to have `first_attempt_success >= 0.95` **and**
multi stalls `~ 0`. Single is `0.9623`. Multi was `0.9565` at 23 cases, but the partial run
has **10 real stall-guard firings across 9 cases** (task 0059 has two). These are genuine
consecutive-identical-code interruptions (`core/orchestrator.py:302-323`, `:415-420`), not an
aggregation artifact. Eight are verifier stalls; `verifier_mode="safe"` discards verifier
sentinel text (`core/orchestrator.py:517-526`, `core/verifier_safe.py:36-41`), so most did not
overwrite a scored answer, but they still fail the written `stalls ~ 0` condition.

The `last_error` strings are mostly recovered intermediate attempts (`evals/run_v5.py:144`,
`core/runtime.py:185-188`), not final failures. At the 23-case snapshot, 14 cases with an
`ERROR` string ultimately passed; only task 0031 (`TypeError`) and 0059 (`UndefinedColumn`)
failed among those.

The remaining repeats follow model-authored code errors (`SecurityError: globals()`,
`NameError: ANSWER`, `KeyError: 'o3'`, wrong SQL column, etc.), not the already-patched
`exit()`/numpy issues. A harness-only change cannot honestly reduce them to `~0` without
masking the metric or tuning prompts, so Gate A is **not passed** on the available evidence.
Because the run stopped at 24/53 and the gate already fails on stalls, no TEST-split run was
started.

A transient `pm2_5_data_jan_2025.csv` appeared in the repository root during the dev run. It was
not part of the pack, was not committed, and was removed before staging.
