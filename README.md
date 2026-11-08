# neuro-air-lite

Hierarchical multi-agent air quality analyst for Hong Kong, built from the
Neuro-Air paper (npj Clean Air 2026) and the `acodercat/Neuro-Air` reference
implementation. One Coordinator plans, five workers share a single persistent
Python runtime, and a Verifier recomputes the key numbers from the database
before the answer is released.

This is the 10-day implementation walkthrough; the plan lives next to this
folder as `Neuro-Air 10-Day Implementation Plan (A to Z).pdf`.

## Requirements

- Python 3.12 (pinned in `.python-version`, installed by `uv`)
- [uv](https://docs.astral.sh/uv/)
- Docker Desktop (PostGIS 16 + 3.4)
- An OpenAI-compatible LLM endpoint. Development target is local Ollama
  (`qwen2.5-coder:14b`); a stronger API model is swapped in for the Day 9
  evaluation via `LLM_MODEL_STRONG`.

## Setup

```powershell
uv sync
Copy-Item .env.example .env    # already present in a fresh checkout of this machine
```

Start the database:

```powershell
docker compose up -d
docker compose ps              # wait for "healthy"
```

Check everything wired up:

```powershell
uv run python scripts/check_env.py
```

The DB and LLM lines are `[skip]` until Docker and Ollama are running; that is
expected on a fresh machine and `--strict` will fail on them once you want a
hard gate. To prove the model can actually produce a runnable code block:

```powershell
uv run python scripts/llm_smoke.py
```

Ollama only serves 4096 tokens of context by default, which truncates the
schema prompt on Day 4. Raise it once per machine and restart `ollama serve`:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH','16384','User')
```

## Layout

```
core/                 llm client, python runtime, orchestrator, prompts, gate
domains/hk/           schema, schema prompt, tools, charts, knowledge base
ingest/               one idempotent script per data source
api/                  FastAPI app, SSE stream, monitoring daemon, static UI
eval/                 PV benchmark tasks, validators, analyze
tests/                pytest
scripts/              environment check and one-off utilities
```

## Database

The schema lives in `domains/hk/schema.sql` and is applied by:

```powershell
uv run python scripts/init_db.py           # create tables + neuro_ro role
uv run python scripts/init_db.py --reset   # drop schema public first, then recreate
uv run python scripts/check_db.py          # Day 1 acceptance checks
```

`init_db.py` also creates the `neuro_ro` role from the credentials in
`DB_URL_RO`, grants it `SELECT` on everything, forces
`default_transaction_read_only=on` and sets a 5 s `statement_timeout`. Ingestion
and `truth_sql` use `DB_URL_ADMIN`; the agent never sees that URL.

14 tables, all following the reference implementation: `air_quality_station`,
`hourly_station_air_quality`, `street`, `road_segment`,
`hourly_road_segment_traffic`, `weather_station`, `hourly_station_weather`,
`osm_feature`, `eia_project`, `eia_habitat`, `traffic_camera`,
`sensitive_area`, `land_use_zone`, `building_height_control`.

`check_db.py` proves the read-only role by attempting INSERT, UPDATE, DELETE,
TRUNCATE, DROP, ALTER, CREATE TABLE and CREATE INDEX, expecting SQLSTATE 25006
for each. It also asserts that geography columns return metres (1107 m for
0.01 degrees of latitude) while a `::geometry` cast returns 0.01 degrees.

## Conventions

- The agent only ever connects with the read-only `neuro_ro` role. Ingestion
  and `truth_sql` use `DB_URL_ADMIN`.
- The LLM writes Python; the numbers come from the runtime. No arithmetic in
  the model's head.
- `YYYY-MM-DD to YYYY-MM-DD` includes both dates in full (end is exclusive at
  the next midnight).
- Distances are in metres because location columns are `geography`; casting to
  `::geometry` silently switches to degrees.

## Troubleshooting

- `docker: command not found` in a terminal that was already open before the
  install: close and reopen it. The installer appends
  `C:\Program Files\Docker\Docker\resources\bin` to the user `PATH`.
- `error getting credentials ... docker-credential-desktop` means the same
  stale-`PATH` problem.
- Ollama answers on `:11434` only after the desktop app (or `ollama serve`) is
  running; start it once per reboot if it is not set to launch at login.

## Status

| Day | Deliverable | State |
| --- | --- | --- |
| 0 | scaffold, deps, compose, env check | done |
| 1 | PostGIS schema, read-only role | done |
| 2 | HK data ingestion | next |
