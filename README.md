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
| 1 | PostGIS schema, read-only role | next |
