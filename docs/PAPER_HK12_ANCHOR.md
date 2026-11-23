# External HK-12 anchor: paper verification, adapter, crosswalk, and honest result

This is the Step 4 external anchor plus the reviewer-requested identifier
crosswalk. It does **not** replace the frozen v5 TEST result in
`docs/V5_TEST_REPORT.md`, and it does **not** reproduce the paper's five-backbone
headline. Only the local `qwen2.5-coder:14b` model was used. The `*_STRONG` keys
are set in `.env` but were neither read as credentials nor used for any call.

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
license from the source root. `tests/test_paper_vendor.py` verifies all 29
non-README hashes; it still passes after the crosswalk work.

## 2. Adapter design and frozen boundaries

`evals/paper_hk12.py` runs each original HK query plus its original
`requirements` through this project's harness. Both arms use the same roles,
runtime, base prompt, temperature `0.2`, `max_steps=20`, and
`verifier_mode=safe`. The base system-prompt hash was
`ff04f301a5eebdc041b8e0e58260ab5cd4011729463f94c785e2201dacea585f`, the same
one used in the v4 runs.

No file under `core/`, `domains/hk/`, `evals/v5/`, or `evals/run_v5.py` was
edited. No role, scorer, tolerance, v5 question, v5 prompt, or split definition
was edited. The vendored paper validators are byte-for-byte copies; they are
made importable by a small runtime shim that supplies:

- `cave_agent.runtime.IPythonRuntime` / `Variable`,
- `domains.HK.db.engine` (the read-only engine),
- `core.types.Turn`,
- `core.validation` (`ValidatorResult`, `is_finite_number`, `compare_numeric`),
- `runtime.retrieve(var)` mapped to this project's runtime namespace.

### 2.1 Identifier crosswalk, not a question or prompt edit

The paper schema numbers the same stations `1..18`; the lite DB uses `19..36`.
The map was taken from the paper's own `benchmark/domains/HK/prompts.py`.
Originally it was applied only to paper ground-truth SQL (`station_id = N`) and
to the one output variable `higher_peak_station_id` declared in the paper
requirements as `16 or 5`. The reviewer then identified a 30.6% adapter
artifact: six of the 12 model-facing questions still contained paper station
IDs while the unchanged base prompt lists local IDs.

The fix is an **identifier crosswalk, not a semantic question or prompt edit**.
`crosswalk_station_ids()` translates only numeric station identifiers in the
model-facing query and requirements text, identically for both arms, using
these forms:

- `station_id: N`
- `station_id = N`
- `station N`
- `higher_peak_station_id (N or M)`

It does **not** translate `district_id`, dates, clock times, station counts,
validator source, or validator SQL shim. In `hk_2_18`, `district_id: 3` is
descriptive only; the validator SQL uses `station_id = 2`, so district ID 3 was
left unchanged. The six ID-free questions are byte-identical after the
crosswalk. Each completed `run.json` records:

- `meta.question_crosswalk`: name, scope, forms, and untouched fields;
- `meta.question_crosswalk_by_task`: original (`raw`) and translated
  (`translated`) model-facing text for all 12 tasks;
- per-case `question_sha256` for the exact text presented to the model.

`tests/test_paper_hk12.py` verifies that every station ID in the 12 questions is
covered, that ID-free questions are unchanged, and that the per-task manifest
matches the task text. `tests/test_paper_vendor.py` verifies that vendored
hashes are unchanged.

Validator smoke check after the crosswalk:

```
uv run python evals/paper_hk12.py --smoke
...
[smoke] tasks=12 unexpected=0
```

All 12 validators imported and their SQL executed on the local read-only DB.

## 3. Runs executed, and the time-cap skip

Raw-ID single arm, completed earlier and retained as the artifact record:

```
uv run python evals/paper_hk12.py --arm single --repeats 3 \
  --out runs/paper_hk12/qwen14b_single --model qwen2.5-coder:14b \
  --base-url http://127.0.0.1:11434/v1
```

Corrected crosswalk single arm, completed in this session:

```
uv run python evals/paper_hk12.py --arm single --repeats 3 \
  --out runs/paper_hk12/qwen14b_single_crosswalk --model qwen2.5-coder:14b \
  --base-url http://127.0.0.1:11434/v1
```

Artifact evidence for the corrected run: 36 case files, `run.json`,
`infra_retries_total=0`, no watchdog abort, `runner_exit=0`, `watcher_exit=0`.

The paired multi arm was **not rerun**. The earlier attempt used the same
settings plus `--verifier-mode safe`, but the full 12×3 multi arm did not finish
within the pre-agreed ~2 hour Step 4 cap. It stopped at **10/36 multi case
files** with no multi `run.json`. Therefore the HK-12 paired single-vs-multi
comparison is skipped, and the partial multi files are not used for a gain,
loss, significance, or ceiling claim. The local multi column below is marked
`not completed`, not `0`.

## 4. Local single arms vs paper matrix

Raw-ID single: **11/36 (30.6%)**, mean 36,483 tokens/case, mean 94.4 s/case,
zero infra retries.

Corrected crosswalk single: **14/36 (38.9%)**, mean 12,072 tokens/case, mean
31.4 s/case, zero infra retries. Each task had 3 repeats.

| Group | Raw-ID single | Crosswalk single |
|---|---:|---:|
| Six ID-bearing tasks | 0/18 | 4/18 |
| Six ID-free tasks | 11/18 | 10/18 |
| All 12 tasks | 11/36 | 14/36 |

Per-task view, with the paper line-up side by side:

| Task | Raw-ID single | Crosswalk single | Paper single (5 backbones) | Paper multi (5 backbones) | Paper failing cells | Group |
|---|---:|---:|---:|---:|---|---|
| hk_1_1 | 0/3 | 0/3 | 5/5 | 5/5 | none | no-ID |
| hk_1_10 | 0/3 | 0/3 | 4/5 | 5/5 | `qwen-2.5-72b/single` | ID |
| hk_1_11 | 3/3 | 3/3 | 5/5 | 5/5 | none | no-ID |
| hk_1_13 | 0/3 | 2/3 | 5/5 | 5/5 | none | ID |
| hk_1_2 | 3/3 | 3/3 | 5/5 | 5/5 | none | no-ID |
| hk_1_5 | 0/3 | 2/3 | 5/5 | 5/5 | none | ID |
| hk_1_6 | 0/3 | 0/3 | 5/5 | 5/5 | none | ID |
| hk_1_7 | 0/3 | 0/3 | 5/5 | 5/5 | none | ID |
| hk_1_9 | 0/3 | 0/3 | 5/5 | 5/5 | none | no-ID |
| hk_2_18 | 0/3 | 0/3 | 5/5 | 5/5 | none | ID |
| hk_2_6 | 3/3 | 2/3 | 5/5 | 5/5 | none | no-ID |
| hk_3_11 | 2/3 | 2/3 | 5/5 | 5/5 | none | no-ID |

The crosswalk recovered the ID-bearing tasks from 0/18 to 4/18, driven by
`hk_1_13` and `hk_1_5`. It did not make all ID-bearing tasks pass, and the
ID-free group changed from 11/18 to 10/18, so 10/18 must not be called a
ceiling. The paper matrix remains at ceiling on these cells: single 59/60,
multi 60/60, 119/120 cells. This external anchor therefore cannot demonstrate
gain; it can only expose failures such as the recorded
`qwen-2.5-72b/single` miss.

### 4.1 `hk_1_1` and `hk_1_9`: repeat-wise first failing checks

Both tasks failed 3/3 even without paper station IDs in the question. The
first failing check recorded by the vendored validator in each corrected-run
case is listed below. The root causes are **not established** here; no further
prompt, validator, scorer, or question tuning was done.

| Task / repeat | First failing check from case artifact |
|---|---|
| `hk_1_1` r1 | `net_mean_aqhi=Decimal('3.5327932098765432'), expected ~3.53 (tol 0.2)` |
| `hk_1_1` r2 | `net_mean_pm25 not set` |
| `hk_1_1` r3 | `net_mean_aqhi=Decimal('3.5327932098765432'), expected ~3.53 (tol 0.2)` |
| `hk_1_9` r1 | `station_peak_hours has non-integer keys/values: {'causewaybay': np.float64(19.0), 'central': np.float64(2.0), 'cw': np.float64(21.0), ...}` |
| `hk_1_9` r2 | `station_peak_hours has non-integer keys/values: {'causewaybay': np.float64(19.0), 'central': np.float64(2.0), 'cw': np.float64(21.0), ...}` |
| `hk_1_9` r3 | `station_peak_hours has non-integer keys/values: {'tapmun': 23.0, 'east': 23.0, 'tungchung': 23.0, 'tuenmun': 23.0, ...}` |

The r1/r3 `hk_1_1` messages are printed verbatim even though the displayed
number is close to the displayed expectation; the validator result was
`success=False`. No claim is made about why it was rejected.

## 5. Honest interpretation and limitations

- The earlier 11/36 result was mostly an identifier artifact, not a clean model
  score. The corrected single score is 14/36. Neither number is a replication
  of the paper's HK result, and neither is evidence for or against multi-agent
  gain.
- The crosswalk was applied only to model-facing station identifiers in the
  HK-12 adapter. It was not applied to the frozen v5 benchmark, prompts, roles,
  scorer, or split. No strong model was used.
- The paired multi arm was skipped under the time cap. No HK-12 win, loss,
  significance, or paired ceiling claim is made.
- The remaining failures are reported as case outcomes. Infra retries were zero
  in the completed single arms; there was no watchdog abort in the corrected
  run.
- No paid API, web model, expert panel, human evaluation, or strong-model run
  was used. No strong key value was printed.
- The raw run folders are gitignored. Their outside-repo archive path, SHA256,
  and per-`run.json` hashes are in `docs/EVIDENCE_MANIFEST.md`; the compact v5
  per-case CSVs are in `docs/results/`.

## 6. Reproduction

```powershell
uv run pytest -q tests/test_paper_hk12.py tests/test_paper_vendor.py
uv run python evals/paper_hk12.py --smoke
uv run python evals/paper_hk12.py --arm single --repeats 3 --out runs/paper_hk12/qwen14b_single --model qwen2.5-coder:14b --base-url http://127.0.0.1:11434/v1
uv run python evals/paper_hk12.py --arm single --repeats 3 --out runs/paper_hk12/qwen14b_single_crosswalk --model qwen2.5-coder:14b --base-url http://127.0.0.1:11434/v1
```

The first single command reproduces the retained raw-ID artifact. The second
reproduces the corrected crosswalk result. The multi command was used in the
earlier attempt but was intentionally stopped by the time cap and was not
rerun; no multi result is claimed.
