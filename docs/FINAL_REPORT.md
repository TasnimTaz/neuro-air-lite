# Final report — Neuro-Air Lite, Day 10

Date: 2026-11-21. Scope: v4 evaluation is frozen. Day 10 adds only the
read-only audit, an alternative subprocess runtime for the API, packaging, and
this write-up. No question, truth value, tolerance, scorer, role, eval script or
in-process evaluation runtime was edited in this session.

## 1. One page in plain language

We built a small Hong Kong air-quality analyst. It reads a read-only copy of a
Postgres/PostGIS database, lets a language model write Python to answer
questions, and shows the numbers and charts in a browser. The database holds
2025 station readings, weather, and map features. A separate, fixed file holds a
deterministic Tai Po reference answer that the browser can show without any AI
or database call.

The paper's multi-agent idea was that several specialist agents plus a verifier
would beat one agent. We tested that with one local 14-billion-parameter model,
20 questions, three repeats, and matched step budgets. It did not win. In v4 the
single agent got 47 of 60 runs right; the multi-agent version got 39. The
difference is not statistically significant. The verifier often repaired a
wrong answer, but it also broke two right answers. Cost was higher for multi.

The Tai Po reference values were reproduced from the local data (for example
NO2 123.2 at 20:00, z 1.91, 14-day maximum 125.5, PM2.5 rise 19.56, rank 1, and
`supports_local_plume=false`). The live agent did not match that reference at
all: 0 of 24 fields in v4. That is an agent-contract failure, not a reference
failure.

We checked the cases that almost never passed. There were no cases with zero
passes in every run: #14, #18 and #20 each passed once in 18 slots, and #15
passed five times. The suspected benchmark defects were not confirmed; those
failures were mostly SQL mistakes by the model, such as using NULL-first
ordering or the wrong join key.

For runtime safety, the API now runs model code in a child process with a
scrubbed environment and a fresh empty working directory. A timeout kills that
child. This is better than the old in-process execution, but it is not a
container: the child is the same OS user and can still reach the network.

Strong-model evaluation was attempted but blocked before any run. The three
`*_STRONG` variables are set to `deepseek-flash`, `https://api.deepseek.com` and
a local key; frozen truth validation passed 20/20, but two smoke calls returned
`402 Insufficient Balance`. No strong run was made, no charge occurred and no
substitute model was used (section 6.2).

## 2. Component status

| Paper / project component | Status | Evidence in this session |
| --- | --- | --- |
| PostGIS schema and read-only role | Done | `README.md:125-144`; live read-only INSERT with `25006` lifted fails with `42501` in `tests/test_subprocess_runtime.py:166` |
| 20-question frozen benchmark and scorer v2 | Done / frozen | `evals/hk_baseline_v2.py` SHA256 `8751bf7c...` from `Get-FileHash` output; no eval file modified |
| Single agent with schema prompt | Done | Base prompt hash `ff04f301a5eebdc041b8e0e58260ab5cd4011729463f94c785e2201dacea585f` at `README.md:1374` |
| Multi-agent coordinator, verifier | Done, no gain reproduced | v4 results and audit at `README.md:1411-1422` |
| Relevance gate / premise traps | Done, weak | Held-out 74.3% advisory at `README.md:1389`; strict trap score **1/10** at `docs/premise_trap_transcripts.md:7` |
| Tai Po deterministic reference | Done, reproduced | `docs/tai_po_reference.json:32,38,60-64,120`; endpoint tests `tests/test_api.py:361-374` |
| Live agent reproduces the 8-field reference | Missing | `runs/reference_compare/day9_v4_single_3x/run.json` aggregate: `field_matches=0/24`, `answer_dict_repeats=0`, `line_chart_passes=0`, `bar_chart_passes=0`; `README.md:1439` |
| Paired weather view / v4 prompt affordance | Done | `scripts/migrate_views.py`, `tests/test_paired_view.py`; v4 hashes at `README.md:1374-1376` |
| OS-level subprocess isolation for API | Done, limited | `core/subprocess_runtime.py:38,44,71,313,416`; tests at `tests/test_subprocess_runtime.py:51-186` and `tests/test_api.py:410` |
| Docker packaging | Done; build and stack health verified | `Dockerfile`, `docker-compose.yml`; `docker_build_exit=0`, both services `healthy`, `/api/health` JSON in §6 |
| Strong model on a stronger LLM | Attempted, blocked by 402 | `deepseek-flash` configured; truth validation `20/20`; two smoke calls `402 Insufficient Balance`; no run artifacts (§6.2) |
| Traffic and emission data case study | Missing | No traffic/emission table is present in the 14-table list at `README.md:125-129` |
| Lag-correlation case study | Missing | Only the single correlation case #14 exists (`docs/never_solved_cases.md:77-142`); it passed 1/18 slots |
| Expert panel / human evaluation | Missing | No expert panel artifact exists in this repository; this report does not claim one |

## 3. Results

### 3.1 Frozen hashes

Recorded at `README.md:1374-1376` and used unchanged for the v4 reruns:

| Item | SHA256 |
| --- | --- |
| Base prompt with live data window | `ff04f301a5eebdc041b8e0e58260ab5cd4011729463f94c785e2201dacea585f` |
| Multi prompts snapshot | `141925a0538b2e039cb0d4ac78589ff0ddd3a99e3e5a27b5cd322976289c23a1` |
| Merged single-role addendum | `2c0a50e48797d11f7b2b8d416447681eb643e482ac39cc6a0117297c941e641d` |

### 3.2 v2 / v3 / v4, both arms

Parsed directly from the six `run.json` files in this session:

| Run | Total | Hold-out | Non-hold-out |
| --- | ---: | ---: | ---: |
| v2 single (`runs/baseline/day4_v2_budget20`) | 46/60 | 12/18 | 34/42 |
| v2 multi (`runs/multi/day5b_v2_budget20_3x`) | 41/60 | 11/18 | 30/42 |
| v3 single | 45/60 | 11/18 | 34/42 |
| v3 multi | 40/60 | 8/18 | 32/42 |
| v4 single | 47/60 | 12/18 | 35/42 |
| v4 multi | 39/60 | 10/18 | 29/42 |

The v4 single arm used merged roles; the multi arm used overwrite verifier mode,
scorer v2, 3 repeats, a 20-step solver budget and `--resume` (`README.md:1405-1409`).
The paired task-level test gave multi 3 wins, 7 losses, 10 ties, exact McNemar
`p=0.3438`, bootstrap mean difference `-0.133` with 95% CI `[-0.300,+0.033]`
(`README.md:1416-1419`). Multi cost 2.23x tokens, 2.34x seconds and 3.85x
calls per case (`README.md:1419-1420`). The verifier audit was wrong→right 25,
right→wrong 2, with pre-verify 16/60 and final 39/60 (`README.md:1420-1422`).

### 3.3 Tai Po reference and agent comparison

The deterministic reference reports `ok=true` and these paper-target values:
`no2_at_2000=123.2`, `wind_speed_2000=0.389`, `matched_hour_z=1.9106`,
`matched_hour_max_prev_14d=125.5`, `network_mean_rise=19.5641`,
`pm25_rank_of_nearest=1`, `supports_local_plume="false"`
(`docs/tai_po_reference.json:4,32,38,60-64,120`).

The v4 single-arm rerun finished 3/3 with `reference_ok=True`, but
`field_matches=0/24`, `answer_dict_repeats=0`, `line_chart_passes=0`,
`bar_chart_passes=0`. All eight fields were `0/3`
(`runs/reference_compare/day9_v4_single_3x/run.json`; `README.md:1439-1453`).
The agent never emitted the exact eight-field `ANSWER` dictionary or the two
chart configs. This is a real failure of the 14-billion-parameter local model
on that contract.

### 3.4 Never-solved case audit

`docs/never_solved_cases.md:55-69` reports **no** all-zero cases. Each of #14,
#18 and #20 passed exactly once in 18 slots; #15 passed five times. The four
suspected cases were judged (a) single defensible reading with the truth SQL
implementing it, not an unstated benchmark convention
(`docs/never_solved_cases.md:133-142,194-199,255-261,320-332`). Specific model
errors: wrong station and direct `station_id` join for #14, any-hour counting
instead of daily-mean counting for #15, PostgreSQL `DESC` placing NULLs first
for #18, and `AVG(roadside - general)` instead of the difference of group means
for #20. Some recorded passes came from a staged numeric `ANSWER` whose
`final_text` was the identical-code stop sentinel or even contradicted the
answer (`docs/never_solved_cases.md:339-343`).

One small-n signal is worth recording separately. Case #15 asks for days whose
*daily average* PM2.5 exceeded 30. Across the six archived runs it passed only
in the multi arm: **5/9** multi slots versus **0/9** single slots
(`docs/never_solved_cases.md:61-69`). The qualitative audit attributes the
repair to the verifier's recompute of the aggregation grain
(`docs/never_solved_cases.md:144-198`). This is one case, and the overall paired
test is not significant, so it is a hypothesis for a stronger-model rerun, not
a claim that the multi arm generally helps.

## 4. Isolation: what it covers and what it does not

`RUNTIME_MODE=subprocess` is the API default (`api/app.py:55,496-500`). The
child is launched with
`subprocess.Popen([sys.executable, "-m", "core.subprocess_runtime", ...],
env=scrubbed, cwd=<fresh temp dir>, close_fds=True)`
(`core/subprocess_runtime.py:419,441-449`). The scrubbed mapping is built at
exec time from `OS_ENV_KEYS` plus `DB_URL_RO` and `PYTHONPATH=ROOT`
(`:55,400-408`). The parent's `os.environ` is never swapped and the old global
lock is gone; this was the Linux/Docker bug in the earlier implementation.
The child connects back over an authenticated `multiprocessing.connection`
Listener/Client on `127.0.0.1` with a random port and random authkey
(`:428-457`). The parent still runs `check_code()` before sending anything.
Output is truncated at 20,000 characters (`:48`), and timeout kills the child
so the next execute starts fresh.

`docker-compose.yml:35-36` passes the whole repo `.env` into the API container
parent, which includes any future `LLM_*_STRONG` key. The exec-time allowlist is
what stops those keys from reaching model-written code; this is now enforced on
both Windows and Linux.

Verified tests (Windows host and Linux container):

- State, manifest, namespace proxy, engine URL proxy:
  `tests/test_subprocess_runtime.py:51,58,66,80`.
- Timeout kills child and later execute works: `:84`.
- Child `os.environ` contains no `DB_URL_ADMIN`, `POSTGRES_*`, `LLM_*`,
  `EMBED_*` or `NEURO_ADMIN_ENV`: `:99,119`.
- On Linux, the child's real `/proc/self/environ` also contains none of those
  keys: `:129`.
- The parent `os.environ` equals its pre-start snapshot while a child starts:
  `:141`.
- No `.env` in the fresh child cwd: `:155`.
- AST checker rejects before any RPC: `:161`.
- Two children do not mix stdout; large output truncated: `:179,205`.
- Live read-only role can SELECT, cannot INSERT after `25006` is lifted: `:212`.
- Infinite loop does not hang the API and does not poison it:
  `tests/test_api.py:410`.
- Concurrency limit 2 replaces single-flight in subprocess mode:
  `tests/test_api.py:238,258`; `api/app.py:115,522`.

Not covered: the child is the same OS user, keeps network access to Postgres and
to the geocoder, and has no container/cgroup sandbox. The AST checker is a guard
rail; the `neuro_ro` grant is the real data boundary. An absolute-path read of
the admin file by deliberately hostile code is not prevented by the environment
scrub. Memory capping is implemented only when `psutil` is installed; `psutil`
was absent in this session (`importlib.util.find_spec('psutil')` returned
`False`), so that branch was not exercised. The in-process evaluator still uses
the unchanged `core/runtime.py`; its poison guard is kept for
`RUNTIME_MODE=inproc`.

## 5. Test output

Full suite on the Windows host, with the database running:

```
uv run pytest -q
........................................................................ [ 30%]
........................................................................ [ 60%]
...........................................................s............ [ 90%]
.......................                                                  [100%]
238 passed, 1 skipped in 28.41s
```

After the budget watchdog was added, the same Windows host reported its own
watchdog file result and then the full suite with the database stopped:

```
uv run pytest -q tests/test_watch_run.py
8 passed in 0.10s

uv run pytest -q
239 passed, 8 skipped in 37.33s
```

The seven extra skips relative to the database-up run are DB-live tests; the
remaining skip is the Linux-only `/proc/self/environ` check. No test was edited
to hide a failure.

For the database-up run shown first, the one skip is the Linux-only
`/proc/self/environ` check (`tests/test_subprocess_runtime.py:129`). The
subprocess file alone on the same host reported `11 passed, 2 skipped in
13.99s`; the second skip was the DB-live test because the database was stopped
at that moment.

The same file was then run inside the freshly rebuilt Linux `api` container
with the database healthy. pytest was already available through the image's
`uv` environment; no throwaway layer was needed. Command and result:

```powershell
$env:DB_URL_RO_DOCKER = uv run python -c "from dotenv import dotenv_values; u=dotenv_values('.env')['DB_URL_RO']; print(u.replace('127.0.0.1','db',1).replace('localhost','db',1))"
docker compose --env-file .env --env-file "$HOME\.neuro-air\admin.env" run --rm api uv run pytest -q -rs tests/test_subprocess_runtime.py
```

```
 Container neuroair-db Running
 Container neuroair-db Waiting
 Container neuroair-db Healthy
 Container neuro-air-lite-api-run-f3e9d32f7949 Creating
 Container neuro-air-lite-api-run-f3e9d32f7949 Created
Installed 1 package in 4ms
.............                                                            [100%]
13 passed in 10.39s
container_test_exit=0
```

Earlier in the session, with the database stopped, the same suite reported
`230 passed, 7 skipped in 35.43s`; the seven skips were DB-live tests. No test
was edited to hide a failure.

## 6. Docker build and stack verification

`docker compose build api` first failed with
`error getting credentials - err: exec: "docker-credential-desktop": executable
file not found in %PATH%`. The local `~/.docker/config.json` had a stale
`credsStore: "desktop"`; after removing it, the unchanged repo build succeeded.
Final command output:

```
docker_build_exit=0
 Image neuroair-api:day10 Built
```

Then `docker compose up -d` was run with both `.env` and the admin env file.
After a first restart caused by a root-owned uv cache, the Dockerfile was fixed
to chown `/home/neuroair`; the rebuilt stack reported:

```
NAME           IMAGE                    STATUS                    PORTS
neuroair-api   neuroair-api:day10       Up 12 seconds (healthy)   0.0.0.0:8000->8000/tcp
neuroair-db    postgis/postgis:16-3.4   Up 6 minutes (healthy)    0.0.0.0:5432->5432/tcp
```

`GET http://localhost:8000/api/health` returned:

```json
{"status":"ok","degraded":false,"busy":false,"runtime_mode":"subprocess","max_concurrent_runs":2,"active_runs":0,"arm_default":"single","arms":["single","multi"],"model":"qwen2.5-coder:14b","timeout_seconds":600.0}
```

The admin env file is excluded from the image by `.dockerignore`; the container
gets `DB_URL_RO` through `DB_URL_RO_DOCKER` and `LLM_BASE_URL` defaults to
`http://host.docker.internal:11434/v1` (`docker-compose.yml`).

### 6.1 Live Ollama response through the frontend endpoint

The Ollama desktop app process was running but its API server was not listening.
Starting `ollama serve` on the host with `OLLAMA_HOST=0.0.0.0:11434` fixed that;
this is a host environment action, not a repo change. From the rebuilt `api`
container, `GET http://host.docker.internal:11434/api/tags` then returned
`200`, and a read-only DB query returned `db_station_count=18`.

A real chat request was then sent to the endpoint the browser UI uses:

```powershell
curl.exe -N -sS -X POST http://localhost:8000/api/chat `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\frontend_chat_request.json"
```

with body
`{"query":"How high above sea level is the Tseung Kwan O air quality station?","arm":"single"}`.
The final SSE event was:

```json
{"arm":"single","answer":20.0,"final_text":"The Tseung Kwan O air quality station is 20.0 meters above sea level.","charts":[],"ok":true,"seconds":15.337,"usage":{"prompt_tokens":6620,"completion_tokens":103,"calls":2},"steps":2,"executions":1,"first_attempt_ok":true}
```

This confirms the UI's SSE path, the subprocess runtime, the read-only DB role
and a real local LLM answer work together end to end. It is still one query, not
a full benchmark rerun, and no browser screenshot was captured.

### 6.2 Strong-model preflight: watchdog, validation and 402 stop

On 2026-11-22 (Sunday, off-peak) the strong-model path was prepared without
touching frozen files. A new budget watchdog was added at `scripts/watch_run.py`
with tests at `tests/test_watch_run.py`. It reads only per-case artifacts under
`<run>/cases/repeat-*-case-*.json`, ignores top-level `run.json` and
`run_meta.json`, and aborts on total tokens, one-case tokens, one-case seconds
or idle time. `--max-not-ok` defaults to off because `ok=false` is an answer
failure, not an infra error. On the Windows host:

```
uv run pytest -q tests/test_watch_run.py
8 passed in 0.10s
```

`.env` was changed only to set `LLM_MODEL_STRONG=deepseek-flash` (the old
`deepseek-v4-flash` is a retired alias), `LLM_BASE_URL_STRONG=https://api.deepseek.com`
and a local key. The key was never printed. Docker Desktop was started and the
existing DB volume was reused; no `init_db.py --reset` was run.

Frozen truth SQL validation initially failed because the DB was not listening
on `127.0.0.1:5432`. After the existing `db` service was healthy, the exact
harness command passed:

```
uv run python evals/run_baseline.py --validate-only
[ok] 20 truth values match their pinned values
validate_exit=0
```

A smoke call used the OpenAI SDK with `model=deepseek-flash`, default provider
thinking, no `extra_body`, and `temperature=0.2` (ignored by thinking mode).
No `max_tokens` was set. It returned `402 Insufficient Balance` twice, with
request IDs `ade5b7e4-3964-4b64-8868-a5dab50ac771` and
`ccbc5b57-b692-439f-952c-629b92cd096d`. Per the pre-agreed abort rule, the
second 402 stopped the sequence. No charge occurred, no `PROVENANCE.md`, mini
run, single run or multi run was created, and no run artifact was staged.

Because no strong-model response was received, the report records no strong
score, no `T` tokens-per-case estimate and no strong/local comparison. DeepSeek
thinking is enabled by default at effort `high`; reasoning appears in
`reasoning_content` and the frozen harness reads only `content`. This run would
therefore not be directly comparable to the local non-thinking `qwen2.5-coder:14b`
runs. Official pricing was read on 2026-11-21; actual billing was never observed.

## 7. Honest conclusion

The multi-agent gain was **not** reproduced with `qwen2.5-coder:14b`: v4 multi
scored 39/60 against single 47/60 and the paired test gave `p=0.3438`. This
shows that this particular local model, prompt set and 20-task benchmark did not
benefit from the coordinator/verifier structure. It does not prove that
multi-agent systems can never help, and it does not test a stronger model.

The v4 paired weather view fixed a real query-construction defect, but it did
not make the multi arm win and did not fix the agent-to-reference contract. The
never-solved audit did not find evidence that the benchmark answers are wrong;
most failures are model-generated SQL mistakes. Two suspicious details remain
honestly recorded: some scorer passes coexist with contradictory prose, and
case #18 has no deterministic tie-break in its truth SQL.

A strong-model run would test whether the same tasks and contracts improve when
the model is large enough to hold the eight-field `ANSWER` schema and the
weather-pairing rules. That run was prepared but blocked by two `402
Insufficient Balance` smoke responses, so no strong score exists. Even if it
had run, it would not have fixed the missing traffic/emission data or
substituted for an expert panel.

## 8. Reproduction

```powershell
uv sync --frozen
uv run pytest -q                       # 237 passed with DB up; skips if DB down
uv run python scripts/migrate_views.py # idempotent; no --reset
docker compose --env-file .env --env-file "$HOME\.neuro-air\admin.env" build api
```

Five-step demo:

```powershell
# 1. Start the stack (set DB_URL_RO_DOCKER in .env with host "db" first).
$env:DB_URL_RO_DOCKER = uv run python -c "from dotenv import dotenv_values; u=dotenv_values('.env')['DB_URL_RO']; print(u.replace('127.0.0.1','db',1).replace('localhost','db',1))"
docker compose --env-file .env --env-file "$HOME\.neuro-air\admin.env" up -d --build

# 2. Check the API and runtime mode.
Invoke-RestMethod http://localhost:8000/api/health | ConvertTo-Json

# 3. Show the deterministic reference (no LLM, no DB).
Invoke-RestMethod http://localhost:8000/api/reference/tai_po | Select-Object ok,observed,anomaly_assessment

# 4. Ask one live question through the UI's SSE endpoint.
curl.exe -N -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" -d '{"query":"What was the PM2.5 reading at Tai Po at noon on 7 January 2025?","arm":"single"}'

# 5. Open the browser UI and the deterministic-reference section.
Start-Process http://localhost:8000/
```

## 9. Unverified items and gaps

- Strong-model evaluation: attempted on 2026-11-22 but stopped after two smoke
  calls returned `402 Insufficient Balance` (section 6.2). The three `*_STRONG`
  variables are set, not empty. No strong score, no billed amount and no
  strong/local comparison exist. Thinking stays at the provider default (ON);
  official DeepSeek pricing was read on 2026-11-21.
- Linux was verified inside the `api` container, not on a separate bare-metal
  Linux host. Windows was verified on the host.
- The local Docker config had a stale `credsStore: "desktop"`; removing it was
  a machine-level fix outside the repo. No credential file is tracked.
- `scripts/dump_db.py` and `scripts/restore_db.py` were added but not executed;
  the existing database volume was reused. Restore is destructive and would
  need a separate confirmation on a real machine.
- Manual browser screenshot: not taken. UI verification was through API and
  static-scan tests (`tests/test_api.py`, `tests/test_ui_static.py`).
- Live chat from inside the container with a real Ollama answer: **verified**
  in section 6.1 for one query. A browser screenshot and a full 20-question live
  UI run were not captured.
- Optional psutil RSS watchdog: code present, but psutil is not installed here.
- No traffic/emission data, no lag-correlation case study, no expert panel, and
  only one small local model. These are project gaps, not hidden results.
- The single-v2 audit slot is the older `day4_v2_budget20` run because
  `runs/baseline/day5b_v2_budget20_3x` is absent; this caveat is recorded at
  `docs/never_solved_cases.md:11`.

## 10. Follow-up isolation fix after the Linux review

Changed only the non-frozen runtime, tests and docs:

- `core/subprocess_runtime.py`: replaced the parent `os.environ` swap and its
  lock with `subprocess.Popen(..., env=scrubbed, cwd=<fresh temp>, close_fds=True)`.
  Communication now uses an authenticated loopback `multiprocessing.connection`
  Listener/Client; timeout-kill, truncation, fresh-child-after-timeout and the
  public proxy surface are unchanged.
- `tests/test_subprocess_runtime.py`: added the real `/proc/self/environ` check
  and a concurrent-start test asserting the parent environment is unchanged.
- `docs/FINAL_REPORT.md` and `README.md`: corrected the OS-specific wording,
  added the host/container pytest outputs, fixed the v4 #18/#20 sentence and
  recorded the one-case #15 multi-arm signal.

Verification in this session: Windows host `238 passed, 1 skipped`; Linux
`api` container subprocess file `13 passed`. No frozen evaluation file was
edited.
