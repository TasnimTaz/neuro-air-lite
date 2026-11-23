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
  (`qwen2.5-coder:14b`). A stronger API model was configured for Day 9 via
  `LLM_MODEL_STRONG`, but it was blocked by `402 Insufficient Balance` and was
  deliberately not used for the Day 11 v5/HK-12 results.

## Setup

```powershell
uv sync
Copy-Item .env.example .env
# Admin credentials live OUTSIDE the repository:
New-Item -ItemType Directory -Force "$HOME\.neuro-air"
Copy-Item admin.env.example "$HOME\.neuro-air\admin.env"
# edit ~/.neuro-air/admin.env: POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, DB_URL_ADMIN
```

The repo `.env` is model-readable, so it must contain only `DB_URL_RO` and the
`LLM_*` settings. The admin DSN and `POSTGRES_*` live in
`~/.neuro-air/admin.env`; set `NEURO_ADMIN_ENV` to use a different path. Do not
copy the admin values back into `.env`.

Start the database:

```powershell
.\scripts\dev_up.ps1           # starts Docker Desktop if needed, waits for healthy
docker compose --env-file "$HOME\.neuro-air\admin.env" ps
```

Docker Desktop does not reliably start with Windows, so `dev_up.ps1` checks the
engine first instead of failing with `pipe not found`. Enable "Start Docker
Desktop when you sign in" in its settings to make the script a no-op.

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
api/                  FastAPI app, SSE stream, static UI
analysis/             deterministic Tai Po reference and drift daemon
evals/                baseline cases, leakage guard, reproducible runner
tests/                pytest
scripts/              environment check and one-off utilities
```

## Database

The schema lives in `domains/hk/schema.sql` and is applied by:

```powershell
uv run python scripts/init_db.py               # create tables + neuro_ro role
uv run python scripts/init_db.py --roles-only  # skip tables, refresh password + grants
uv run python scripts/init_db.py --reset       # DROP schema public CASCADE first
uv run python scripts/check_db.py              # Day 1 acceptance checks
```

`--reset` destroys every ingested row, so after Day 2 use `--roles-only` when you
only need the role and grants reapplied.

`init_db.py` also creates the `neuro_ro` role from the credentials in
`DB_URL_RO`, grants it `SELECT` on everything, forces
`default_transaction_read_only=on` and sets a 5 s `statement_timeout`. Ingestion
and `truth_sql` use `DB_URL_ADMIN`; the agent never receives that URL.
`domains/hk/db.py` is the only sanctioned way to build either engine
(`make_ro_engine()` and `make_admin_engine()`); the read-only profile pins
`pool_size=5`, `max_overflow=10`, `pool_pre_ping=True`, `connect_timeout=5` and
passes `statement_timeout` as a startup option so a changed role default cannot
lift it.

The admin DSN and `POSTGRES_*` live outside the repository, in
`~/.neuro-air/admin.env` by default (`NEURO_ADMIN_ENV` overrides the path).
`scripts/init_db.py`, `scripts/check_db.py` and `ingest.common.load_env()` read
that file; the repo `.env` holds only `DB_URL_RO` and the LLM settings. This
blocks accidental and ordinary API-level reads of `.env` (pandas and numpy can
read a file even when the AST checker blocks `open`). A deliberate absolute-path
read of `~/.neuro-air/admin.env` from model code is still possible until the Day
10 subprocess runs with a scrubbed environment; the claim is limited to that.

The Postgres superuser password comes from `POSTGRES_PASSWORD` in the admin file.
`dev_up.ps1` passes `--env-file` automatically; if you run compose by hand:

```powershell
docker compose --env-file "$HOME\.neuro-air\admin.env" up -d
```

The password only applies when the `pgdata` volume is first created; changing it
later needs `ALTER USER` or `docker compose down -v`.

14 tables, all following the reference implementation: `air_quality_station`,
`hourly_station_air_quality`, `street`, `road_segment`,
`hourly_road_segment_traffic`, `weather_station`, `hourly_station_weather`,
`osm_feature`, `eia_project`, `eia_habitat`, `traffic_camera`,
`sensitive_area`, `land_use_zone`, `building_height_control`.

`check_db.py` proves the read-only role in two layers:

1. INSERT, UPDATE, DELETE, TRUNCATE, DROP, ALTER, CREATE TABLE and CREATE INDEX
   each fail with SQLSTATE 25006, the read-only transaction default.
2. With `SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE` the same writes
   fail with SQLSTATE 42501, so the GRANT layer is proven independently of the
   session default.

It also proves the timeout (`pg_sleep(6)` cancelled at 5.0 s with 57014), asserts
that geography columns return metres (1107 m for 0.01 degrees of latitude) while
a `::geometry` cast returns 0.01 degrees, and warns that `neuro_ro` can disable
its own `statement_timeout` with `SET statement_timeout = 0` - no GRANT can stop
that, so the real wall-clock guarantee has to come from the Day 10 process
supervisor, not from Postgres.

## Data sources (Day 2)

Day 2 fills the six tables the later days actually query. Every source below was
probed from this machine first; the probe script is `scripts/probe_sources.py`.

### Source Matrix

| Data | Source | Resolution | Coverage | Target Table |
| --- | --- | --- | --- | --- |
| Stations (18) | Curated EPD locations | Metadata | 15 general, 3 roadside | `air_quality_station` |
| AQHI (1-11) | EPD monthly CSV (`https://www.aqhi.gov.hk/epd/ddata/html/history/<year>/<yyyymm>_Eng.csv`) | Hourly | Full 2025 | `hourly_station_air_quality` |
| Pollutants (PM2.5, PM10, NO2, O3, SO2, CO) | EPD "Past 24-hour Pollutant Concentration" XML (`24pc_Eng.xml`) via data.gov.hk **Historical Archive** | Hourly | Full 2025 | `hourly_station_air_quality` |
| Temperature, humidity, pressure | Open-Meteo ERA5 archive (`https://archive-api.open-meteo.com/v1/archive`) | Hourly | Full 2025 | `hourly_station_weather` |
| Wind speed, gust, direction | HKO "latest 10-minute mean wind" CSV via data.gov.hk **Historical Archive** | 10 min -> hourly vector mean | Full 2025 | `hourly_station_weather` |
| Schools, hospitals, worship, parks, industry | OpenStreetMap via Overpass API | Point | Current extract | `osm_feature`, `sensitive_area` |
| Building footprints (Tai Po AOI) | OpenStreetMap Overpass (2.5 km around 22.4468, 114.1754) | Point centroid | Tai Po area | `osm_feature` |

### Ingested Row Counts

| Table | Rows | Notes |
| --- | --- | --- |
| `air_quality_station` | 18 | 15 general, 3 roadside stations |
| `hourly_station_air_quality` | 157,536 | 18 stations × 8,760 hours = 157,680 expected; exactly 144 hours missing from upstream source |
| `weather_station` | 18 | Paired 1-to-1 with AQ stations and mapped to nearest HKO weather station |
| `hourly_station_weather` | 157,680 | 18 stations × 8,760 hours (complete 2025 year) |
| `osm_feature` | 17,413 | 5,707 buildings (Tai Po AOI), 3,727 leisure/parks, 3,267 industrial, 2,605 public, 1,418 religious, 687 cemeteries, 2 water |
| `sensitive_area` | 3,515 | 1,985 educational institutions, 1,418 places of worship, 112 hospitals |

### Execution & Idempotency

Run the ingestion scripts in this order:

```powershell
uv run python -m ingest.aq          # stations, AQHI, pollutants
uv run python -m ingest.weather     # temperature, humidity, pressure (ERA5)
uv run python -m ingest.weather_hko # wind (HKO observations)
uv run python -m ingest.osm --source overpass # OpenStreetMap POIs & Tai Po buildings
uv run python scripts/check_data.py # Day 2 acceptance checks
```

Each script is strictly idempotent:
- Downloads and API responses are cached under `ingest/cache/` (gitignored).
- Overpass queries are cached as JSON files (`overpass_schools.json`, etc.) so re-runs never repeat network queries.
- Database writes use transactions and upserts (`ON CONFLICT DO UPDATE SET ...`), so re-running converges to the exact same row counts.

> [!CAUTION]
> **Database Reset Warning:** Once data is ingested, **NEVER** run `python scripts/init_db.py --reset` (it executes `DROP SCHEMA public CASCADE` and destroys all ingested data). Always use `python scripts/init_db.py --roles-only` if you need to refresh roles or grants.

### Known Gaps & Characteristics

1. **144 missing AQ hours:** The EPD source omits a handful of station-hours (13,386 rows in January vs 13,392 theoretical 24×18×31, etc.).
2. **2025-11-23 18:00 HKO wind feed gap:** Inside the Tai Po fire case-study baseline window, HKO omitted wind reporting for that single hour (`wind_speed` and `wind_direction` are NULL at Tai Po, while temperature and air quality are present). Agents should handle occasional missing baseline hours via SQL/pandas `dropna()` or aggregations without error.
3. **CO nulls at 7 stations:** Only 11 of the 18 stations report CO measurements. The remaining 7 stations report NULL (63,474 NULL values overall).
4. **Occasional pollutant nulls:** Periodic instrument calibration or maintenance results in nulls (e.g., 5,398 PM2.5 nulls and 4,428 NO2 nulls across 157k rows).
5. **HKO wind nulls & calm hours:** 3,558 nulls occur where HKO station feeds skipped snapshots. When wind speed is 0 (calm), wind direction/degree is recorded as NULL.
6. **ERA5 temperature spatial variation:** Due to ERA5's reanalysis grid resolution (~9-11 km), temperatures vary relatively little across Hong Kong stations.

### Deviations from the Implementation Plan

1. **Pollutants come from the archived XML, not the live feed:** The live feed only serves the last 24 hours, so a historical 2025 ingest requires data.gov.hk's monthly archive bundles.
2. **CO is not multiplied by 1000:** The published data dictionary confirms all pollutants are reported in ug/m3. Scaling CO would produce an unphysical 600 mg/m3 instead of the real 0.5-0.9 mg/m3.
3. **Temperature, humidity, and pressure from ERA5:** HKO Open Data does not expose an hourly historical temperature/humidity API (only astronomical tides). ERA5 fills these columns, while wind uses real HKO weather station observations.
4. **Curated station coordinates (~300 m precision):** Taken from official EPD station maps.
5. **Weather station mapping & wind vectors:** Each AQ station is paired with its nearest HKO automatic weather station via `hko_name` in `domains/hk/stations.py` (Tai Po is paired with `Tai Po Kau` AWS). Wind speed is computed as an unweighted vector mean of 10-minute snapshot readings, with calm readings counted as 0.0 km/h.
6. **HKO wind compass conversion:** HKO reports 16 compass directions rather than degrees. Vector means are computed and converted to degrees, and `wind_direction` is mapped to the schema's 8-point enum.
7. **OpenStreetMap POIs & Tai Po AOI buildings:** Full Geofabrik 137k-building PBF is heavily throttled/unreachable on this network. Overpass loads all Hong Kong schools, hospitals, clinics, places of worship, parks, and industrial areas, plus building centroids within 2.5 km of Tai Po (Wang Fuk Court, 22.4468 N, 114.1754 E) for the Day 8 fire case study.
8. **OSM building proximity count vs paper numbers:** Our 2026 OSM snapshot includes 4,434 buildings within 2 km of Tai Po (Wang Fuk Court) and 1,153 residential buildings within 1.5 km (the paper quotes 1,090 total and 293 residential). Cause not determined; OSM snapshot date (2026 vs ~2023) and building filtering/aggregation methods differ (e.g., individual housing blocks vs estates). In Day 8 this will be framed as an OSM proximity screening count rather than claiming an exact match to the paper's numbers.
9. **Sensitive area restrictions:** `HEALTH_CARE_INSTITUTION` in `sensitive_area` is strictly restricted to `amenity=hospital` (clinics, doctors, dentists are kept only in `osm_feature`).
10. **Empty tables reserved for later days:** `street`, `road_segment`, `hourly_road_segment_traffic`, `eia_project`, `eia_habitat`, `traffic_camera`, `land_use_zone`, and `building_height_control` remain empty until their respective implementation days.

### Notes for Day 4 (Schema Prompt & Baseline)

- **Only populated tables in prompt:** The Day 4 schema prompt must only expose the 6 populated tables (`air_quality_station`, `hourly_station_air_quality`, `weather_station`, `hourly_station_weather`, `osm_feature`, `sensitive_area`). Prompting on empty tables causes the LLM to write queries that return 0 rows.
- **Sensitive receptors:** Schools and hospitals must be queried from `sensitive_area` (`EDUCATIONAL_INSTITUTION`, `HEALTH_CARE_INSTITUTION`).
- **Spatial queries & geography cast:** `osm_feature.geometry` and `sensitive_area.geometry` are `geometry(4326)`. To measure distances in metres rather than degrees, the model must cast geometries to `::geography` (e.g. `ST_DWithin(geometry::geography, ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography, distance_in_metres)`).

### Sanity Values (Algorithm 1 Verification)

The Tai Po fire case study checks reproduce the paper exactly:

| Metric | Target | Ingested DB | Ratio vs Pre-fire Mean | Status |
| --- | --- | --- | --- | --- |
| Tai Po NO2 (2025-11-26 20:00) | 123.2 ug/m3 | 123.2 ug/m3 | - | exact match |
| Tai Po AQHI (2025-11-26 20:00) | 5 (MODERATE) | 5 (MODERATE) | - | exact match |
| Tai Po 24h post-fire PM2.5 Peak | 54.6 ug/m3 | 54.6 ug/m3 | 2.05x (pre: 26.68 ug/m3) | exact match |
| Tai Po 24h post-fire NO2 Peak | 123.2 ug/m3 | 123.2 ug/m3 | 2.52x (pre: 48.90 ug/m3) | exact match |
| Tai Po Wind (2025-11-26 20:00) | Near calm | 0.389 m/s from 207° (SW) | - | consistent with Tolo basin narrative |

## Single agent (Day 3)

Day 3 is one agent that turns a question into Python, runs it against the
read-only database, and answers from the printed output. It is the Day 3
capability baseline; the Day 9 reference is the frozen 20-case v2 set in
`evals/`, not the 9/10 smoke run below.

```
core/llm.py       AsyncOpenAI client (Ollama by default), token/latency USAGE
core/runtime.py   in-process exec namespace + AST security checker + close()
core/agent.py     worker loop: one code block per turn, nudge, step budget
core/prompts.py   common instructions: role, runtime manifest, execution rules
core/env.py       agent-side .env loader; admin DSN is returned, not exported
domains/hk/schema_prompt.py  curated schema, HK knowledge base, worked examples
domains/hk/tools.py  build_runtime(): engine + pd + np + text + geocode + charts
domains/hk/viz.py    ECharts config builder (NaN/None-safe)
```

### How the isolation actually works

The AST checker rejects forbidden imports, calls, bare names (`__builtins__`),
dunder attributes, attribute names that lead back to the process environment
(`pd.io.common.os.environ` is blocked at `.os`), and write/DDL/session SQL
anywhere in a statement (`SELECT 1; SET statement_timeout = 0`). The SQL pattern
matches at a statement boundary and requires the object of the verb, so
`print('Set of stations:', 3)` and `print('Update: mean PM2.5')` pass while
`'DELETE FROM t'` and `'SET statement_timeout = 0'` do not.

An AST blacklist can always be bypassed (`'DR' + 'OP TABLE x'`), so it is a guard
rail, not the boundary. The real guarantees are:

1. The agent engine is `neuro_ro`: inserts fail at 25006 (read-only transaction),
   and at 42501 (missing GRANT) even with the default lifted. Both are asserted
   in `tests/test_runtime.py`.
2. The admin DSN is not in the repo `.env` and is never exported to the agent.
   `core.env.load_agent_env()` copies only `DB_URL_RO` and `LLM_*` from `.env`
   into `os.environ` and scrubs `DB_URL_ADMIN`/`POSTGRES_*` if the parent shell
   exported them. `admin_url()` reads the admin-only file outside the repo and
   returns the DSN to the caller without exporting it. This stops accidental and
   API-level `.env` reads; a deliberate absolute-path read of
   `~/.neuro-air/admin.env` is still possible until the Day 10 subprocess.

Ingest and schema scripts use `ingest.common.load_env()`, which loads the repo
`.env` and then the admin-only file; `scripts/check_db.py` and
`scripts/init_db.py` use `core.env.load_admin_env()` explicitly. Agent-side entry
points (`build_runtime()`, `scripts/check_agent.py`, `tests/`) use the restricted
loader.

### Prompt

The schema description is curated, not introspected: introspection only reports
enum *type* names, not labels, and loses units and provenance. Every table,
column, enum label, unit and source (ERA5 for temperature/humidity/pressure, HKO
for wind) is written out. `validate_schema()` compares that description with
`information_schema` at prompt-build time and raises if a column is missing or a
new one appeared undocumented, so the prompt cannot silently drift.

The prompt also injects the real coverage window and the 18-station inventory
(name and code) read from the database, uses `Asia/Hong_Kong` for "now"
(Windows needs `tzdata`, added to the dependencies), and states the NULL rules:
CO is NULL at 7 of 18 stations, `wind_degree`/`wind_direction` are NULL in calm
hours, and NULL means "no measurement", never 0. Station type is in the schema;
the model queries it when a question needs roadside/general filtering.

### Agent loop

- Exactly one executable block per turn. `python`, `py` and unlabelled fences
  run; a ```sql block shown with the SQL is ignored instead of being executed as
  Python.
- One no-code nudge on turn 1; a later turn without a code block is the final
  answer.
- `first_attempt_ok` is keyed on the first *executed* block, so the nudge does
  not count a correct first program as a failure.
- Token counts are the `core.llm.USAGE` delta around the loop (zero for the
  scripted fake LLM in tests).
- The `engine` pool is disposed by `runtime.close()`; every script and test does
  this in a `finally`.

### Acceptance run

```powershell
uv run pytest                                   # unit + live-DB isolation tests
uv run python scripts/check_agent.py --cross-check
uv run python scripts/check_agent.py --question "Mean PM2.5 at Tai Po on 2025-11-25?"
uv run python scripts/check_agent.py --cross-check --only 8 --show-trace
```

`check_agent.py` first proves the five isolation claims (admin DSN absent from
`os.environ`, password absent, RO DSN present, pandas trick blocked, INSERT
rejected with 42501), then runs ten questions against a ground-truth SQL query
and compares the agent's `ANSWER` variable with the expected value. It reports
pass rate, first-attempt rate, mean steps, mean latency and tokens.

| # | Question | Truth |
| --- | --- | --- |
| 1 | mean PM2.5 at Tai Po on the latest available day | 21.60 ug/m3 |
| 2 | max NO2 at Causeway Bay, Nov 2025 | 211.70 ug/m3 |
| 3 | mean AQHI at Central, Jun 2025 | 2.45 |
| 4 | hours PM2.5 > 50 at Yuen Long, 2025 | 163 |
| 5 | mean wind speed at `wx_taipo`, Nov 2025 | 2.51 m/s |
| 6 | max wind gust at `wx_tapmun`, 2025 | 42.78 m/s |
| 7 | mean temperature at `wx_shatin`, Aug 2025 | 27.48 C |
| 8 | `EDUCATIONAL_INSTITUTION` within 1500 m of Tai Po | 53 |
| 9 | mean O3 at Tung Chung, Oct 2025 | 58.94 ug/m3 |
| 10 | `ROADSIDE` station count | 3 |

The plan phrases question 1 as "yesterday"; the database is historical
(2025 only), so the case asks for the latest day that has data. Coverage is in
the prompt, and an out-of-range window must be reported as "no data", not 0.

**Baseline result** (`qwen2.5-coder:14b`, local Ollama, 62 tests green at the time):

```
RESULT: 9/10 passed | first-attempt 9/10 | mean steps 2.1 | mean 12.0s | tokens 52,729
```

The one failure is semantic, not infrastructural: asked for mean O3 at Tung
Chung, the model wrote `AVG(aq.pm2_5) AS mean_o3` and reported **14.37** instead
of **58.94**. The SQL executed, the code was first-attempt correct, and the
printed number is simply the wrong pollutant. No SQL guard or schema assertion
can catch that; it is the failure mode the Day 5 Verifier and the Day 9
single-vs-multi comparison exist to measure. The earlier run also failed the
spatial question because the prompt only showed keyword-style bind parameters;
adding the SQLAlchemy 2.0 dict form (`conn.execute(text(sql), {"k": v})`) fixed
that case (53), which is why the baseline is 9/10 rather than 8/10.

### Day 3 deviations and limits

1. **Curated schema instead of introspection.** Introspection is used only as a
   consistency check (`validate_schema`), because it loses enum labels, units
   and provenance.
2. **The admin DSN is outside the repository.** The plan's snippet calls
   `load_dotenv()`, which exports every key; a module that already holds `os`
   (pandas does) could then read the admin password from model code. The repo
   `.env` now holds only `DB_URL_RO` and the LLM settings; the admin values live
   in `~/.neuro-air/admin.env` (or `NEURO_ADMIN_ENV`).
3. **`pytest-asyncio` + `asyncio_mode = "auto"`.** Without the plugin the async
   tests are silently skipped; without the mode the marker is unknown.
4. **`tzdata` added.** `ZoneInfo("Asia/Hong_Kong")` fails on a bare Windows
   Python.
5. **No process-level sandbox yet.** `exec()` and `redirect_stdout` are
   process-wide, so concurrent requests in one process are unsafe; Day 10 moves
   execution to a subprocess with an RPC boundary.

## Schema prompt, tools & baseline (Day 4)

Day 4 gives the single agent what a domain analyst needs beyond SQL: a prompt
that knows Hong Kong's units, stations, AQHI/AQO context and sources; a
host-side geocoder; and a host-side chart builder. The implementation gate is
the static + live tool probes and the v1 smoke run. The **reported reference
baseline is v2**, a separate 20-question set; v1 stays in the repo as a
contaminated smoke test and is not used for the Day 9 comparison.

```
domains/hk/schema_prompt.py    curated schema, KNOWLEDGE_BASE, three examples
domains/hk/tools.py            geocode(): Nominatim -> Lands Department ALS fallback
domains/hk/viz.py              EChartsConfigManager.line/bar/to_json/save_html
evals/hk_baseline.py           smoke set v1 (contaminated; not the baseline)
evals/hk_baseline_v2.py        frozen 20-case reference, six hold-outs
evals/run_baseline.py          prompt sha256 + model + repeats -> runs/baseline/*.json
evals/compare_arms.py          offline re-score + task-level paired comparison
scripts/check_day4.py          tool probes + v1 smoke run + runs/day4_baseline.json
tests/test_prompt_leakage.py   12-token leakage guard over v1 and v2
```

### Prompt

- `core/prompts.py` holds the domain-neutral part: role, injected variables, the
  SQLAlchemy bind-parameter rule, the date convention and the read-only rule.
  `domains/hk/schema_prompt.py` appends HK rules, the knowledge base, three
  worked examples and the curated schema.
- The prompt is assembled by concatenation, not `str.format`; JSON braces are
  literal. This removes the Day 3 `KeyError: '"station"'` failure mode.
- `KNOWLEDGE_BASE` carries AQHI bands (`10+` = SERIOUS), both AQO regimes
  (2022-01-01..2025-04-10 and from 2025-04-11), WHO 2021 guidelines, the
  ug/m3 unit rule (CO is not multiplied by 1000), and a source URL for each.
- `validate_schema()` still asserts the curated description against
  `information_schema` at prompt build time; the dynamic data-window block lists
  the real 2025 range and all 18 stations by name and code.
- Measured size with the live data window: **9,990 characters, about 2,497
  tokens**, sha256 `ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048`
  (9,243 characters without it). The three examples use a chart on
  `osm_feature`, a geocode + bind-parameter nearest-weather query, and a
  `sensitive_area` school radius at Sham Shui Po (800 m); none reproduces a v2
  question or truth SQL, and none uses a v2 station/radius/date/threshold
  combination.
- Entity resolution, NULL, empty-result and invalid-premise rules are explicit:
  fuzzy names are spacing-normalized and must print the single match (or stop on
  0/>1), a missing measurement is "not available" (not 0), and a nonexistent
  station or an out-of-coverage date must be reported as unanswerable.
- Institution counts use `sensitive_area`, the authoritative school/hospital/
  worship inventory; `osm_feature` is described as general map data. This is
  deliberate: the two sources have different coverage, and the v2 truth uses
  `sensitive_area`.

### AQHI "10+" repair

EPD publishes the top band as `10+`. The first ingest parsed it as 10, so 47
serious station-hours were labelled VERY HIGH. `ingest/aq.py` now maps `10+` to
`aqhi = 11`; `aqhi_level = 'SERIOUS'` is the authoritative label. Re-ingesting
the 12 monthly CSVs changed 47 rows (all April 2025), left the total at 157,536,
and `scripts/check_data.py` asserts that `aqhi > 10` and `SERIOUS` agree. This is
why v1 smoke case 3 expects 47.

### Geocoder

`geocode(address)` runs in the host process and is injected as a variable, not
an import. Model code can call it; `import httpx`, `requests`, `http` and
`aiohttp` are now blocked by the AST checker.

- Nominatim is primary. It is queried **without** `countrycodes=hk`: on the
  public instance that filter returns an empty list for every Hong Kong address
  (verified 2026-11-09). The HK bounding box (22.15-22.58 N, 113.83-114.51 E) is
  the actual filter.
- The Lands Department ALS (`https://www.als.gov.hk/lookup`) is the fallback.
  It is authoritative for HK street addresses but is a fuzzy matcher: it returns
  a candidate even for nonsense. The result therefore includes the ALS `score`
  (`None` for Nominatim) so the caller can judge. A string that produces no
  candidate at all (`asdfghjkl qwerty`) does raise `ValueError`; the README does
  not claim that every impossible string raises.
- One request per second across both providers, `lru_cache` for the process
  lifetime, and a copy returned so callers cannot mutate the cache.

### Charts

`charts.line(title, x, series, y_name)` and `charts.bar(...)` append an ECharts
`option` and return its index. `_clean` converts `NaN`, `None`, `+/-inf` and
numpy scalars to strict JSON: non-finite values become `null`, which ECharts
renders as a gap. `to_json()` is strict JSON; `save_html()` writes a standalone
page for manual review. The live chart probe plots Tai Po's 30 November days and
checks the config, not just the model's prose.

### Baseline v1 is a contaminated smoke set

The former `15/15` headline is withdrawn. Three of v1's worked examples were
the same questions and SQL as three v1 cases:

| v1 case | Contaminated example | What was duplicated |
| --- | --- | --- |
| 7 | old example 2 | geocode HKU -> nearest air-quality station |
| 8 | old example 3 | places of worship within 2 km of Yuen Long |
| 12 | old example 4 | days with Yuen Long daily PM2.5 above the current AQO |

`evals/hk_baseline.py` stays in the repo as **smoke set v1**, and
`scripts/check_day4.py` still runs it, but it is not the Day 9 baseline. Its
first full run was 13/15 and a rerun with the same prompt was 15/15:

| v1 run | Passed | Note |
| --- | --- | --- |
| first full run | 13/15 | #1 used `max(datetime)` instead of the day floor; #5 used `extract(dow) < 5` |
| rerun, temperature 0.2 | 15/15 | variance, not evidence of a clean ceiling |

Those two misses are real failure modes (latest-day flooring, Sunday=0 in
PostgreSQL `dow`), but the 13-15/15 spread cannot be used to claim a multi-agent
gain: v1 had a ceiling effect and prompt leakage.

`tests/test_prompt_leakage.py` now guards both sets: it lower-cases the text,
strips SQL literals, numeric literals and `wx_` station codes, and fails if any
question or truth SQL shares a **12-token run** with the assembled prompt. It
runs on every v1 and v2 case. The prompt examples are a chart on `osm_feature`,
a geocode + bind-parameter nearest-weather query, and a Sham Shui Po
`sensitive_area` school radius; none reproduces a v2 question or its SQL, and
the leakage test checks the normalized assembly rather than trusting that claim.

### Baseline v2 (Day 9 reference)

`evals/hk_baseline_v2.py` freezes 20 real-user questions, four in each category
(`retrieval`, `temporal`, `spatial`, `statistics`, `multi_table`). Design:

- 17 of 20 need two or more tables or a spatial join;
- four fuzzy entity names (`Shatin`, `tung chung`, `causeway bay`,
  `Central/Western`) force an explicit lookup and must print the match;
- two NULL traps: CO at Sha Tin (never measured there) and a real
  station-hour whose PM2.5 is NULL. Answering 0 fails; "not available" passes;
- two invalid premises: the nonexistent Kennedy Town station and January 2026
  (outside the 2025 coverage). The only pass is a final sentence that says the
  question cannot be answered **and** `ANSWER` unset;
- six hold-outs (`#2, #6, #11, #14, #17, #20`) are marked and are not used to
  tune the prompt.

Run it with:

```powershell
uv run python evals/run_baseline.py --validate-only
uv run python evals/run_baseline.py --repeats 3
uv run python evals/run_baseline.py --arm multi --repeats 3
uv run python evals/run_baseline.py --arm multi --repeats 3 --resume runs/multi/<run>
uv run python evals/run_baseline.py --model <model> --base-url <openai-compatible-url> --api-key-env <ENV_NAME>
```

The runner records the model, the prompt sha256, the pinned and recomputed truth
values, per-case steps/tokens/seconds, first-attempt status, code blocks and
outputs in `runs/baseline/<timestamp>/run.json`, plus the exact prompt in
`prompt.txt`. Every `(case, repeat)` is also written atomically as soon as it
finishes under `cases/`, so a killed process loses at most one case; `--resume`
skips finished files and rebuilds `run.json` at the end. `run_meta.json` records
the prompt hashes, model, budget, scorer and verifier mode and is checked before
a resume.

The multi arm writes to `runs/multi/<timestamp>/` and also records
`multi_prompts.txt`, the plan and every worker's step/token ledger. It snapshots
`answer_pre_verify` before the verifier runs, so the same artifact supports a
"multi without verifier" ablation. `--verifier-mode overwrite|flag|none`
selects the verifier behaviour; the default remains `overwrite`. A truth
mismatch stops the run instead of silently moving the benchmark. `--no-holdout`
is for prompt work and must not be used for the frozen reference.

Scoring is versioned. Scorer `v1` is the frozen Day 4 matcher; `v2` only extends
the accepted no-data/unanswerable wording and leaves every numeric rule
unchanged; `semantic` is a broader audit view. The runner stores `pass_v1`,
`pass_v2` and `semantic_pass` for every case, and `--scorer` selects the primary
one (`v2` by default). Historical artifacts are re-scored offline by
`evals/compare_arms.py`. Scorer v2 was frozen before the official Day 5b run; an
aborted first attempt that exposed one omitted pre-registered phrase (`could
not`) is retained under `runs/multi/day5b_v2_budget20_3x_pre_scorer_fix/` and is
not an official result. See `docs/multiagent_failure_log.md`.

#### Frozen v2 results (scorer v2)

Both artifacts use the same frozen prompt, model, questions and truth SQL:

| Artifact | Max steps | Strict | Semantic | Repeats | Hold-out | Tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **20-step matched budget** | **20** | **46/60 (76.7%)** | **46/60** | **15/16/15** | **12/18 (66.7%)** | **588,512** |
| 10-step preliminary | 10 | 44/60 (73.3%) | 46/60 | 16/15/13 | 11/18 (61.1%) | 631,106 |

The **20-step** run is the single-arm matched-budget reference for Day 9:
`runs/baseline/day4_v2_budget20/run.json` (generated
2026-11-10T14:41:21Z), model `qwen2.5-coder:14b`, prompt sha256
`ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048`,
three repeats. Truth validation passed 20/20 before the run. The committed
`docs/baseline_v2_summary.json` records the model, hashes, per-case counts and
both scorer versions. The prompt sha256 includes the live data-window text
injected by `get_system_prompt`, so it cannot be recomputed offline from the
repository alone.

| Repeat | Passed | Hold-out | Non-hold-out | First attempt | Mean steps | Tokens | Wall-clock |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 15/20 | 4/6 | 11/14 | 20/20 | 2.75 | 189,771 | 239.3s |
| 2 | 16/20 | 4/6 | 12/14 | 18/20 | 2.85 | 203,110 | 272.8s |
| 3 | 15/20 | 4/6 | 11/14 | 19/20 | 2.80 | 195,631 | 243.2s |
| **Mean** | **76.7%** | **66.7%** | **81.0%** | **57/60** | **2.80** | **588,512** | **12.6s/case** |

| # | Category | Difficulty | Hold-out | Pass / 3 |
| ---: | --- | --- | :---: | ---: |
| 1 | retrieval | simple | no | 3/3 |
| 2 | retrieval | simple | yes | 3/3 |
| 3 | retrieval | medium | no | 3/3 |
| 4 | retrieval | simple | no | 3/3 |
| 5 | temporal | medium | no | 1/3 |
| 6 | temporal | medium | yes | 3/3 |
| 7 | temporal | medium | no | 3/3 |
| 8 | temporal | simple | no | 3/3 |
| 9 | spatial | medium | no | 3/3 |
| 10 | spatial | medium | no | 3/3 |
| 11 | spatial | hard | yes | 3/3 |
| 12 | spatial | medium | no | 3/3 |
| 13 | statistics | medium | no | 3/3 |
| 14 | statistics | hard | yes | 0/3 |
| 15 | statistics | medium | no | 0/3 |
| 16 | statistics | medium | no | 3/3 |
| 17 | multi_table | hard | yes | 3/3 |
| 18 | multi_table | hard | no | 0/3 |
| 19 | multi_table | hard | no | 3/3 |
| 20 | multi_table | hard | yes | 0/3 |

By category: retrieval 100.0%, temporal 83.3%, spatial 100.0%, statistics
50.0%, multi_table 50.0%. By difficulty: simple 100.0%, medium 83.3%, hard
50.0%. The four repeat-zero cases are still `#14`, `#15`, `#18`, `#20`; `#5`
now passes only once. The 20-step budget did **not** remove the prompt-level
weather-join failure, and `#18`/`#20` still fail for distinct SQL reasons.
`docs/failure_log.md` has the code-level evidence; the official Day 5b
multi-arm measurement and verifier audit are in
`docs/multiagent_failure_log.md`.

The 10-step artifact (`runs/baseline/20261110_191651/run.json`) remains the
historical Day 4 reference. Its v1 score was 44/60; a semantic audit counted two
correct NULL answers (`#3` repeat 2 and `#16` repeat 3) and gave 46/60. The
frozen score is not rewritten: v1 is kept, and scorer v2 only accepts the wider
no-data wording. In the 20-step run `#3` and `#16` pass 3/3, so its v1 and v2
scores are both 46/60. Do not compare the 10-step table with a 20-step
multi-agent arm.

### Day 4 deviations and limits

1. **Curated schema, not raw DDL.** Kept from Day 3; `validate_schema()` is the
   guard. The prompt renders one compact line per table; SQL type names are
   omitted because the model needs units, enum labels and join keys, not
   `..._enum` identifiers.
2. **`geocode()` returns an extra `score`.** Needed because ALS is fuzzy; without
   it a caller cannot distinguish a building match from a junk match.
3. **AQHI is encoded as 11 for EPD's `10+`.** The column is an integer and the
   enum has SERIOUS; recomputing AQHI from pollutants is explicitly forbidden
   in the prompt.
4. **The baseline is a Python module plus a JSON artifact**, not a notebook: the
   values are pinned, the truth SQL is re-run, and the runner records the prompt
   hash and model so a later run can prove whether the reference changed.
5. **The two live tool probes are not part of v2.** They check tool use (did the
   model call `geocode`, did it build a chart with 30 x-points), which is a
   different question from numeric accuracy.
6. **v1 remains as a smoke set.** It is explicitly contaminated and its score is
   not reported as the baseline; its only role is a quick Day 4 tool check.
7. **Two prompt defects were fixed before the freeze, not after it.** Diagnostic
   runs exposed (a) a fuzzy example that searched `%shatin%` although the
   database stores `Sha Tin`, and (b) a school example that used `osm_feature`
   while the truth uses the authoritative `sensitive_area` inventory. Both were
   corrected generically before the official repeats; no v2 question or truth
   SQL changed. The official hash is `ab5ef7e...`; the pre-freeze diagnostics
   are kept under `runs/pre_freeze_prompt_bug/` and are not part of the reported
   table. Details and evidence are in `docs/failure_log.md`.
8. **The frozen v2 prompt still has a weather-join ambiguity.** The domain rule
   says “join hourly tables on `station_id`” while the pairing rule only states
   that weather codes are `'wx_<air quality station_code>'`; no worked example
   joins the two hourly tables. This was deliberately left unchanged so the v2
   baseline measures the defect. The v3 prompt did state the join explicitly and
   both arms were rerun at the same 20-step budget; the result was still
   negative (v3 single 45/60 vs multi 40/60), and direct `station_id` joins still
   appear. The rule is necessary for clarity but not sufficient; see the v3
   section below and `docs/multiagent_failure_log.md`.

## Hierarchical multi-agent (Day 5)

`core/orchestrator.py` implements the plan's coordinator → workers → verifier
pipeline. The coordinator sees only the question and returns a JSON array of at
most six steps; `synthesis_report` is forced to the end and a malformed planner
response falls back to the conservative four-role checklist. Every worker gets a
fresh chat history and the **same shared `PythonRuntime`**, so variables staged by
one worker are visible to the next. The roles live in `domains/hk/roles.py`.

The Day 5b v2 arms use the same frozen v2 base prompt, cases, model and scorer.
The base prompt is the byte-identical prefix of every worker system prompt; the
multi arm appends a delimited role addendum and the shared-runtime rules, while
the single arm keeps the base prompt and its task hint. The v3 rerun below
replaces the shared base prompt for both arms and adds the merged role text to
the single arm as a fairness control. The addendum contains
procedure only: no v2 question, truth value, station id or `wx_` worked example.
The prompt-leakage test covers those strings too. The run command is:

```powershell
uv run python evals/run_baseline.py --arm multi --repeats 3
uv run python evals/compare_arms.py --single runs/baseline/day4_v2_budget20 --multi runs/multi/<run>
```

### Budget ledger

The 20-step solver budget is split as follows:

| Part | Steps | Meaning |
| --- | ---: | --- |
| Producer pool | 11 | data acquisition, retrieval, analysis and meteo workers |
| Synthesis reserve | 3 | the final `synthesis_report` step |
| Verifier reserve | 6 | independent raw-database audit |

The coordinator's planning call is **orchestration overhead**: it is recorded as
`planner_calls`, `planner_tokens` and in `total_calls`, but it is not charged
against the 20 solver steps. This preserves the plan's 11-step producer pool; a
later all-call sensitivity check can use a 19-step solver cap. The runner records
both `steps` (solver) and `calls` (planner + solver), so the distinction is visible
in every artifact.

A runtime stall guard stops a worker that tries to execute the identical code
block twice. It does not supply SQL or an answer; it only prevents one role from
consuming the whole producer pool in a retry loop. The verifier still gets its
reserve and can overwrite `ANSWER` only after a fresh recomputation. The guard
count is stored per case as `stall_count`; the single arm has no guard in the
frozen baseline, so the single count is zero by construction, not a comparison.

`--verifier-mode overwrite` is the default Day 5 behaviour. `flag` runs the same
verifier but restores the synthesis `ANSWER` afterwards; `none` skips the
verifier and gives its six steps back to the producers. `answer_pre_verify` and
`final_text_pre_verify` are snapshotted before the verifier in every mode.

### Day 5b official result (3 repeats, matched 20-step solver budget)

The official multi run uses the same frozen v2 questions, truth SQL, base prompt,
model, temperature and scorer as the single arm. It finished all 60 case-runs
(20 tasks × 3 repeats) after one resume; the artifact records `resumed: true`.
The multi process had exited at 34/60 saved cases, so the in-flight case was
rerun exactly once.

| Arm | Budget | v1 | v2 (primary) | Semantic | Repeats | Hold-out | Tokens | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Single v2 (Day 4) | 20 | 46/60 (76.7%) | **46/60 (76.7%)** | 46/60 | 15/16/15 | 12/18 (66.7%) | 588,512 | 755.3 |
| Multi v2 (Day 5b) | 20 + planner | 37/60 (61.7%) | **41/60 (68.3%)** | 41/60 | 15/13/13 | 11/18 (61.1%) | 3,065,202 | 6,877.5 |

The multi arm is **5 case-runs lower** than the single arm. The task-level
comparison is **2 wins, 8 losses, 10 ties** (`#15`, `#18` wins; losses `#4`,
`#5`, `#7`, `#8`, `#13`, `#16`, `#17`, `#19`). The exact McNemar/binomial test
gives **p=0.1094**, and the paired task bootstrap is **−0.083**, 95% CI
**[-0.183, +0.033]**. This is a negative/null result: the point estimate is
worse, but 20 tasks do not reject the null. It is not evidence of a multi-agent
gain.

The verifier is doing most of the work. Before the verifier the multi arm passes
only **13/60 (21.7%)**; after `overwrite` it passes **41/60 (68.3%)**. The audit
is **28 wrong→right, 0 right→wrong, 13 right→right, 19 wrong→wrong**, with 37
changed answers. The 0 right→wrong count uses the stored pre-pass flag and still
hides a text regression in `#16` repeat 1 (correct refusal sentence, `NaN`
`ANSWER`, then an ambiguous-station message). The final multi score is a
worker-plus-verifier result, not evidence that the worker handoff alone works.

Cost per case is **5.21× tokens, 9.11× wall-clock and 4.45× calls**. The multi
run used 688 solver steps (525 producer + 163 verifier), 34 stall events, 60
planner calls and 28,687 planner tokens. The planner call remains outside the
20-step solver cap, so this is a lower bound on an all-call budget. All 19 failed
case-runs are classified in
[`docs/multiagent_failure_log.md`](docs/multiagent_failure_log.md): 13 are the
weather-pairing/join cluster, 10 involved the stall guard, and the rest are stale
or non-finite answers, wrong aggregation, or entity resolution.

### Day 5b v3 prompt rerun (negative)

The v2 result above is frozen. v3 changed only the shared base prompt: it states
the explicit join `weather_station.station_code = 'wx_' ||
air_quality_station.station_code` and the alias form. The multi role addendum
is byte-identical to v2. The single arm uses `--single-roles`, which appends a
delimited, procedure-only merged-role addendum, so the comparison is multi
topology plus role procedures versus a single agent with the same base and
procedural text. The v2 questions, truth SQL, tolerances, scorer and `ANSWER`
contract are unchanged.

| Item | Single v3 | Multi v3 |
| --- | ---: | ---: |
| Base prompt sha256 | `538596df...` | `538596df...` |
| Role addendum sha256 | `2c0a50e4...` (merged) | `141925a0...` (unchanged) |
| v2 score | **45/60 (75.0%)** | **40/60 (66.7%)** |
| v1 / semantic | 41/60 / 45/60 | 38/60 / 40/60 |
| Repeats | 14/16/15 | 16/11/13 |
| Hold-out / non-hold-out | 11/18 / 34/42 | 8/18 / 32/42 |
| Tokens / seconds | 1,015,175 / 2,680.7 | 3,184,398 / 6,466.5 |
| Mean steps / calls | 2.70 / 162 | 11.63 / 758 |
| Tokens per case | 16,920 | 53,073 |
| Stalls | 0 | 37 |

The multi arm is **5 case-runs lower** than the v3 single control. The
task-level comparison is **2 wins, 6 losses, 12 ties** (wins `#5`, `#15`;
losses `#3`, `#4`, `#6`, `#8`, `#16`, `#17`); exact McNemar **p=0.2891**, paired
task bootstrap **-0.083**, 95% CI **[-0.183, +0.017]**. This is a negative/null
result. The v3 explicit join did not improve the aggregate: v3 multi is 40/60
versus v2 multi 41/60, and v3 single is 45/60 versus v2 single 46/60. `#14` and
`#20` still fail, and `#17` is 0/3 for multi.

Cost per case is **3.14x tokens, 2.41x seconds and 4.68x calls** relative to the
v3 single control. The lower ratios versus v2's 5.21x/9.11x do **not** mean the
multi arm got cheaper: the v3 single denominator is larger (1.73x tokens and
3.55x seconds versus v2 single) because of the merged role text. Absolute multi
cost is roughly unchanged (3.18M tokens, 6,466.5s, 758 calls, 37 stalls).

The verifier audit is **17/60 pre -> 40/60 final**, with **26 wrong->right, 3
right->wrong, 14 right->right, 17 wrong->wrong** and 32 changed answers. The
three right->wrong cases are real harm: `#18` repeat 1 changed the correct 4.24
m/s to 3.71 m/s, `#3` repeat 2 replaced a correct no-data refusal with the stall
sentinel, and `#16` repeat 3 replaced a correct no-data sentence with a station
ambiguity. All 20 failed case-runs are classified in
[`docs/multiagent_failure_log.md`](docs/multiagent_failure_log.md); 13/20 still
involve weather pairing, 16/20 hit the stall guard, and 8 ended in the stop
sentinel.

#### Matched non-hold-out verifier-mode comparison

The `flag` and `none` runs were deliberately limited to the 14 non-hold-out
tasks (42 case-runs). They must be compared with the **same 14-task single
subset**, not with the 60-case single total. The correct matched comparison is:

| Matched 14 non-hold-out tasks | Case-runs | Rate |
| --- | ---: | ---: |
| Single v3 (filtered to the 14 tasks) | **34/42** | **81.0%** |
| Multi v3 + verifier `flag` | **12/42** | **28.6%** |

The old pairing of `45/60 (75.0%)` single with `12/42 (28.6%)` flag mixes a
60-case denominator with a 42-case denominator and is not a valid comparison.
The matched task-level test is single 10 wins, flag 0 wins and 4 ties (exact
McNemar `p=0.0020`). The flag audit is `0 wrong->right | 0 right->wrong | 12
right->right | 30 wrong->wrong`: the verifier changed 30 answers but the flag
mode restores the synthesis answer, so the final score stays at the
pre-verifier 12/42.

`none` is **abandoned/incomplete**, not a result. The artifact
`runs/multi/day5b_v3_verifier_none_nonholdout_3x/` contains 21 case files and no
`run.json`; it has no aggregate score and must not be reported as one. Without
verifier authority the multi worker pipeline scores **12/42 (28.6%)**, so the
verifier is the main solver in the multi arm. The verifier's authority, not the
worker handoff, is what moves the score.

Reproduce the matched comparison without editing frozen eval code:

```powershell
uv run python scripts/filter_run_artifact.py `
  --input runs/baseline/day5b_v3_budget20_3x --non-holdout `
  --out runs/single_verified/inputs/single_v3_nonholdout_3x
uv run python evals/compare_arms.py `
  --single runs/single_verified/inputs/single_v3_nonholdout_3x `
  --multi runs/multi/day5b_v3_verifier_flag_nonholdout_3x `
  --out runs/multi/day5b_v3_verifier_flag_nonholdout_3x/compare_vs_single_matched.json
```

#### Optional-preferred single + verifier control

`scripts/run_single_verified.py` is a non-frozen control arm: the v3 single
solver gets 14 steps, then the unchanged multi verifier role and prompt get 6
reserved steps (`verify_reserve=6`), for the same 20-step total. It runs the 14
non-hold-out tasks three times with the same frozen scorer and the same
`compare_arms.py`. It does **not** replace the official single or multi arms and
does not change the scorer.

| Matched 14 non-hold-out tasks | Case-runs | Rate |
| --- | ---: | ---: |
| Single v3 (filtered) | 34/42 | 81.0% |
| **Single + verifier (14 + 6)** | **35/42** | **83.3%** |
| Multi v3 + verifier `overwrite` | 32/42 | 76.2% |
| Multi v3 + verifier `flag` | 12/42 | 28.6% |

The three single-verified repeats were 11/14, 12/14 and 12/14 (range
78.6%-85.7%). The verifier audit was **36/42 pre -> 35/42 final**, with
`0 wrong->right | 1 right->wrong | 35 right->right | 6 wrong->wrong` and 6
changed answers. The verifier therefore did not create the one-run gain over
single; it slightly harmed the solver's pre-verify result.

Against single, single-verified wins 1 task, loses 0 and ties 13 (exact McNemar
`p=1.0000`; paired bootstrap `+0.024`, 95% CI `[0.000, +0.071]`). It costs
2.66x tokens, 3.35x seconds and 2.50x calls per case. Against multi-overwrite,
single-verified is 35/42 versus 32/42; multi-overwrite wins 1 task, loses 4 and
ties 9 (exact McNemar `p=0.3750`; bootstrap `-0.071`, 95% CI
`[-0.167, +0.024]`); the multi-overwrite arm costs 2.04x its tokens, 2.13x its
seconds and 2.44x its calls. The single-versus-multi-overwrite comparison is
34/42 versus 32/42; multi-overwrite wins 2 tasks, loses 4 and ties 8
(`p=0.6875`). These differences are not statistically distinguishable at this
sample size. The comparison artifacts are
`runs/single_verified/inputs/compare_single_vs_flag_matched.json`,
`compare_single_vs_sv.json` and `compare_sv_vs_multi_overwrite.json`
(gitignored).

#### Day 5 preliminary 1-repeat (historical)

The earlier usable 1-repeat multi run used the same frozen prompt and finished at
2026-11-10T16:15:58Z. It scored **11/20 with scorer v1 (55.0%)** and **13/20
with scorer v2** after an offline re-score: `#3` and `#16` gave the correct “no …
available” answer but the v1 text rule did not accept their wording. A 3-repeat
attempt was started with the same prompt hash but the background process ended
during repeat 1 at case `#19`; no `run.json` was produced, so that attempt is not
used. The official 3-repeat result above supersedes this signal.

| Arm | Budget | v1 | v2 | Repeats | Hold-out | Mean steps | Calls | Tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Single v2 (Day 4) | 20 | 46/60 (76.7%) | 46/60 | 15/16/15 | 12/18 (66.7%) | 2.8 | 168 | 588,512 |
| Multi v2 (1-repeat) | 20 + planner | 11/20 (55.0%) | 13/20 | 11/20 | 3/6 (50.0%) | 11.3 | 246 | 978,423 |

The 1-repeat multi run used 177 producer steps, 49 verifier steps and 20 planner
calls (9,456 planner tokens). It added no case that the single arm failed and
regressed on `#3`, `#4`, `#16` and `#17`; `#5`, `#14`, `#15`, `#18` and `#20`
failed in both arms. `#4` left a stale `ANSWER` after the verifier said “not
available”, and `#14` had a correct synthesis value (3.51) that the verifier
discarded. The weather-join failures were unchanged.

### Day 5 deviations and limits

1. **20 v2 cases, not the PDF's literal 15.** The frozen v2 set has four cases in
   each of five categories and includes the Day 4 hold-outs; using it keeps the
   single/multi comparison on the same questions. The PDF's “15 queries” is a
   minimum, not a different benchmark.
2. **The planner call is separate from the 20 solver steps.** This is recorded
   in every artifact as `planner_calls` and `total_calls`; it preserves the
   plan's 11-step producer pool. An all-call 19-step sensitivity run is still
   required before any claim that includes orchestration overhead.
3. **The role prompts were smoke-tested before the full multi runs.** Two
   non-hold-out smoke cases (`#1`, `#5`) exposed a stalled worker and an
   over-eager knowledge worker; the generic stop/read-the-error and
   measured-value rules in `domains/hk/roles.py` were added before the full
   1-repeat run and unchanged for Day 5b. The v2 questions and truth SQL were not
   changed, and the multi prompt hash is recorded in the artifact.
4. **Scorer v1 is primary history; v2 is primary for the Day 5b rerun.** Scorer
   v2 only broadens the accepted “no data / unanswerable” wording; every numeric
   rule is unchanged. v1, v2 and semantic scores are all stored per case, and
   `evals/compare_arms.py` re-scores saved artifacts rather than trusting the
   historical `pass` field. The rule was versioned and frozen before the official
   Day 5b multi run; no scorer change is allowed after that result exists.
5. **The weather-join ambiguity was the next controlled change, and it did not
   rescue the result.** The v2 multi arm uses the same frozen schema prompt as
   the single arm, so it cannot repair the missing
   `weather_station.station_code = 'wx_' || air_quality_station.station_code`
   rule. The v3 prompt adds that rule to the base prompt (not to a role
   addendum). Both arms were rerun, but v3 multi is 40/60 versus the v3 single
   control's 45/60; direct id-space joins still appear. The prompt fix is not a
   capability or verifier fix.
6. **The single v2 reference has no merged-role addendum.** The multi arm appends
   procedure-only role text after the base prompt; the frozen single arm predates
   that contract, so the Day 5b contrast is “multi topology + role procedures”
   versus “single base prompt,” not topology alone. The v3 rerun adds the same
   procedural role text to the single arm and reruns both arms; the v2 artifact
   is not rewritten. The v3 single control scores 45/60, one below the v2 single
   reference; it is not a replacement for that frozen reference.

## Relevance gate, embeddings and knowledge base (Day 6)

Day 6 adds three new components without touching the evaluated prompt:

```
core/embeddings.py          async Ollama /v1/embeddings client + on-disk cache
core/gate.py                cosine relevance gate over 18 capability sentences
domains/hk/kb.py            Markdown chunking, kb_search(), build_runtime_with_kb()
evals/gate_set.json         30 in-scope, 30 out-of-scope, 10 vague in-scope
evals/premise_traps.json    10 premise traps with expected behaviour
scripts/calibrate_gate.py   live held-out threshold calibration (theta=0.54)
scripts/check_premise_traps.py  validator and live single-agent trap runner
```

### Embeddings

`EmbeddingClient` calls the OpenAI-compatible `/v1/embeddings` endpoint on local
Ollama. It uses `EMBED_MODEL` (default `nomic-embed-text`), `EMBED_BASE_URL`
(fallback `LLM_BASE_URL`) and `EMBED_API_KEY` (fallback `LLM_API_KEY`). There is
no PyTorch or sentence-transformers dependency. Vectors are cached at
`ingest/cache/embeddings/<model>/<sha256(prefix + text)>.json` (gitignored;
override with `EMBED_CACHE_DIR`), so repeated gate/KB scope text makes no
request. The cache key includes the model, the task prefix and the text, and
cache writes use atomic replace.

`nomic-embed-text` is asymmetric, so retrieval queries and indexed documents use
different prefixes: `EMBED_QUERY_PREFIX` defaults to `search_query: ` and
`EMBED_DOC_PREFIX` defaults to `search_document: `. The trailing space is part
of the prefix and of the cache key. Calibration and runtime both call the same
`embed_query` / `embed_documents` methods; set either variable to an empty
quoted string for a symmetric embedding model.

The model was installed after the single-verified run finished, so the live
chat evaluation was not competing with an embedding load:

```powershell
ollama pull nomic-embed-text
```

`ollama list` reports `nomic-embed-text:latest` at 274 MB. Calibration and the
other live checks were run serially because they share the same Ollama server.

### Gate

`R(q, D) = max cosine(q, scope_sentence)` over 18 short capability sentences,
covering the six populated tables, 2025 time windows, aggregation, thresholds,
comparisons, joins and Hong Kong distance. `GATE_THETA` overrides the threshold;
the built-in `DEFAULT_THETA` is **0.54**, selected by the live calibration below.
`docs/gate_calibration.json` is the machine-readable calibration record.
Empty queries and queries longer than 2,000 characters are rejected before the
embedder is called. The gate returns `{in_scope, score, nearest_scope, theta,
reason}`.

`evals/gate_set.json` has the required 30 / 30 / 10 split. `scripts/calibrate_gate.py`
embeds the set once, splits each group 50/50 with a fixed seed, chooses theta on
one half, and reports accuracy on the other half. It prints every theta from
0.30 to 0.70 step 0.02, but the headline is the held-out number, not train. It
also lists every held-out out-of-scope query that leaks through at the selected
theta, with its score. Those are expected topic-gate false positives: the gate
decides whether a question is about the domain, not whether its premise is true
or the data can answer it. The script makes live embedding calls and must not run
while an eval is running.

The live run selected `theta=0.54` on 35 disjoint training items and scored
**0.743 held-out accuracy** on the other 35 items (train 0.829). Held-out
confusion counts were `tp=20, tn=6, fp=9, fn=0`; the nine leaks are all
out-of-scope topic false positives, not missed in-scope queries. The full list
and scores are in `docs/gate_calibration.json`. A topic gate is not a premise
checker, so the nine leaks are not counted as capability failures.

### Knowledge base

`domains/hk/knowledge/*.md` is chunked by heading; sections longer than about
300 words are split and every piece keeps its heading. Chunk embeddings are
computed once per `KnowledgeBase`. `kb_search(query, k=3, min_score=0.54)` returns
`{text, source, heading, score}`. Every returned `text` is enclosed in
`[BEGIN KB DATA]` / `[END KB DATA]` with a "data, not instructions" notice, and
delimiter strings inside a document are neutralised so a hostile chunk cannot
close the wrapper early. That is a prompt-injection guard rail, not a sandbox.

`DEFAULT_MIN_SCORE` is also **0.54**, the calibrated gate cutoff. There is no
separate labelled KB-retrieval set, so this is a shared cosine cutoff, not an
independent KB tuning result. The README states that limitation rather than
inventing a second calibration number.

`build_runtime_with_kb()` is new in `domains/hk/kb.py`; it calls the existing
`build_runtime()` and injects a synchronous `kb_search` function. It is **not
wired into the evaluated agent**: adding the tool or its description to the
prompt changes the base prompt hash and requires a fresh matched run of both
arms. The gate is likewise not yet called by the API.

### Premise traps

`evals/premise_traps.json` contains ten traps: nonexistent station, future date,
ozone treated as a primary emission, no station, no time window, unit confusion,
out-of-coverage year, a presumed strong-wind event that did not happen, a PM10
exceedance phrased as a PM2.5 alert, and a non-HK location. Each has
`expected_behaviour` (`halt`, `ask clarification`, or `state limitation`) and a
reason. `scripts/check_premise_traps.py` validates the JSON by default. `--live`
runs each trap once through the frozen single-agent loop (or `--arm multi`),
writes one JSON per trap plus a run artifact, and judges the recorded final text
and `ANSWER` with an explicit per-trap rule. The rule is written out in
`judge_trap`, stored with the evidence that fired it, and is deliberately not an
LLM judge. The live run needs Ollama and the database.

The live single-arm run used `max_steps=20` and passed **1/10** traps:
`ask clarification 0/5`, `halt 1/4`, `state limitation 0/1`. Only `trap-10`
(Guangzhou outside Hong Kong coverage) passed. The failures are substantive, not
formatting: `trap-01` said “No data available for Chek Lap Kok” without saying
the station is absent from the inventory; `trap-02` answered `0` for a 2026
future date; `trap-03` said the schema has no ozone-emissions table instead of
correcting the secondary-pollutant premise; `trap-04` did not ask for a station
or time; `trap-05` invented a time period and answered `63.08`; `trap-06` did not
identify the missing CO unit; `trap-07` did not mention 2019; `trap-08` claimed
the 2025-11-26 fire was outside the 2025 data range; and `trap-09` did not
distinguish PM10 from PM2.5. The raw text and rule evidence are in
`runs/premise_traps/day6_live/run.json`. This is a diagnostic, not a numeric
benchmark result.

### Day 6 tests and limits

The Day 6 tests are offline: a deterministic fake embedder and a fake OpenAI
client, no Ollama and no database. They cover gate decisions and the boundary,
empty/overlong rejection before embedding, query/document prefix separation in
the cache, cache hits and corrupt-cache refetching, chunking and ranking,
`min_score`, DATA wrapping, injection text staying inside the wrapper, the
runtime tool injection, and the 30/30/10 and 10-trap data shapes.

```powershell
uv run pytest -q
```

The evaluated base prompt and multi prompts are unchanged by Day 6. Wiring the
gate into `api/` or `kb_search` into the knowledge-retrieval role is a later,
hash-changing step.

## API and demo UI (Day 7)

`api/app.py` is the FastAPI application; `api/static/` is the browser UI. The UI features an executive atmospheric intelligence dashboard and an interactive **Live Multi-Agent Collaboration Matrix** that visualizes inter-agent communication, glowing data packet transfer, and real-time execution in a dark-themed glassmorphism interface.

```powershell
uv run uvicorn api.app:app --reload --port 8000
# open http://127.0.0.1:8000
```

### Standalone Instant UI Preview

To inspect and demonstrate the full 6-agent coordination animation, executive KPI cards, and dynamic ECharts visualizations without spinning up PostgreSQL or loading Ollama weights into VRAM:

```powershell
uv run python preview_ui.py
# open http://127.0.0.1:8000 (or http://127.0.0.1:8000/static/matrix_preview.html)
```

`preview_ui.py` and `matrix_preview.html` are **synthetic UI previews only**:
they use fixed mock values, perform no live model or database query, and show a
permanent red `SYNTHETIC DEMO DATA` banner. They are not imported by
`api.app` and must not be used as evidence for the live demo or screenshots.

`POST /api/chat` takes one JSON object and returns `text/event-stream`:

```json
{"query": "Which station had the highest 2025 hourly PM2.5 reading?", "arm": "single"}
```

`arm` is `single` (default) or `multi`; `query` is also accepted as `question` or
`message` for small clients. The SSE event names are `start`, `plan` (multi
only), `worker_start`, `worker_stall`, `code`, `output`, `agent_final`, `final`,
`error` and `done`. The public `final` event is one object with `answer`,
`final_text`, the accumulated ECharts configs, usage, elapsed seconds and the
topology's step/verifier counters.

In multi-agent mode the narrative analytical synthesis generated by the
synthesis worker is preserved as primary text only when the verifier leaves
`ANSWER` unchanged. If the verifier changes it, `final_text` is the verifier's
final text (consistent with the public `ANSWER`), `synthesis_draft` carries the
pre-verifier narrative, and the UI shows a written "verifier changed the stored
answer" banner plus the draft in a secondary block. The public payload also
carries `verifier_status` (`not_run`, `confirmed`, `changed`, `mismatch` or
`failed`), `answer_pre_verify`, and `verifier_audit_text`; the UI renders the
real outcome instead of a hard-coded "Verified" badge.

The inner orchestrator's textual `final` is renamed `agent_final` so the two cannot be confused. Both arms receive the same `ANSWER_HINT` task suffix as the evaluated runs; this is task plumbing, not a change to the frozen base prompt. A 15 s SSE comment keeps an idle connection alive while a model call is in flight.

Each request gets a fresh `PythonRuntime` and closes it in a `finally` block.
The API only calls `build_runtime()`; it never reads `DB_URL_ADMIN`.
`create_app()` calls `load_agent_env(scrub=True)`, so admin-only keys are removed
from the API process even if the parent shell exported them.

`PythonRuntime.execute` uses process-wide `contextlib.redirect_stdout`, so the
endpoint is intentionally single-flight: a second query gets `429` with
`Retry-After: 2` instead of racing the first trace. This is a guard rail until
Day 10 moves execution into a subprocess. The whole run is wrapped in a
600-second timeout; a timeout emits an `error` event.

`asyncio.wait_for` cancels the async task but cannot stop a worker thread that
is already inside `PythonRuntime.execute`. The API therefore tracks
`runtime.running`. If a timeout or client disconnect leaves that thread running,
the app is marked poisoned: `/api/health` returns `degraded: true` and every
later `POST /api/chat` returns `503 {"error": "runtime still executing; restart
the server"}`. The process must be restarted; this is not a substitute for the
Day 10 subprocess isolation.

The static UI posts the question, parses the SSE stream, shows the trace, renders
the final text and `ANSWER`, and draws every runtime chart with ECharts. It has no
admin credentials and no database code of its own. Model/DB-derived text is
HTML-escaped before the small markdown-style formatter runs; the remaining
`innerHTML` templates are static or use a `safe...` value. A static test scans
`api/static/app.js` and `index.html` for unescaped `${...}` markup
interpolations.

The Day 7 tests use a scripted fake LLM and `PythonRuntime` instances; they do not
call Ollama or Postgres:

```powershell
uv run pytest -q tests/test_api.py
```

`scripts/run_demo.py` is the live transcript runner for the same API. It reads
the five prompts in `scripts/demo_queries.md`, sends the first four to the
single arm and the Tai Po case study to the multi arm, and saves the raw SSE
bytes, parsed events, final payload, chart configs, verifier status and errors
under `runs/demo/` (gitignored). The browser UI consumes the same stream.

The live browser/API demo was run on 2026-11-18 against
`http://127.0.0.1:8000` with `scripts/run_demo.py`. Health before and after was
`status: ok`, `degraded: false`, `busy: false`. All five requests returned HTTP
200; the index page was 200 (14,911 bytes) and contained the AI notice. The
first four single-arm prompts returned event order
`start -> code -> output -> final -> done` with `verifier_status: not_run`.
Their answers were 20.0 m, 18.25 ug/m3 with one `Daily Mean` line chart (30 x
values, one line series, y-axis `PM2.5 (ug/m3)`), 13 hours, and 53 institutions.
No SSE `error` event occurred.

The fifth prompt, the Tai Po fire case study on the multi arm, exposed a real
failure. Event order was `start -> plan -> six worker_start cycles with seven
code/output pairs -> agent_final -> final -> done`. The data-acquisition worker
joined `hourly_station_air_quality` to `hourly_station_weather` on `station_id` and got
0 rows, even though the schema prompt already says to join through
`weather_station.station_code = 'wx_' || air_quality_station.station_code` and
never to use the air-quality `station_id` as a weather `station_id`
(`domains/hk/schema_prompt.py:150-157`). Later workers used wrong dates and
called the forbidden `exit()`. The final payload had `answer: null`,
`charts: []`, `ok: true`, `verifier_status: "confirmed"` and
`verifier_changed_answer: false`; its final text was `The question cannot be
answered from the data because there is no data available for the specified
station and date range.` It used 15 calls and 66,197 tokens. The verifier
banner was `confirmed` and its audit text was `The task cannot be completed due
to the security restrictions preventing the use of the exit() function. Please
provide an alternative approach to handle the situation where no matching air
quality station is found.` It therefore confirmed a wrong no-data
answer, and no SSE `error` event was emitted. The correct code-prefix join
returns 24 rows for Tai Po on 2025-11-26; the 20:00 row is PM2.5 35.7, NO2
123.2, AQHI 5, wind 0.389 m/s from 206.6 deg (SW).

This is the paired-weather join failure, not evidence that the data is absent.
The Day 7 recommendation was a read-only DB view exposing the tested code-prefix
mapping plus a regression test; v4 implements that as
`paired_hourly_weather` and also supersedes the earlier prompt freeze for
`domains/hk/schema_prompt.py`. The transcripts are in
`runs/demo/day8_live/manifest.json` and `runs/demo/day8_live.log` (gitignored).

## Day 8: deterministic Tai Po reference and daemon

`analysis/tai_po_reference.py` is the non-LLM reference for the Wang Fuk Court,
Tai Po fire case. It runs fixed SQL against the read-only role, issues
`SET TRANSACTION READ ONLY` on its connection as a second guard, and compares
the observed values with the sanity values used by `scripts/check_data.py`:

```powershell
uv run python -m analysis.tai_po_reference --compact
```

The paper-target checks are printed as paper / ours / delta, without tuning the
fixed queries:

| Paper target | Paper | Ours | Delta |
| --- | ---: | ---: | ---: |
| Matched-hour NO2 z-score | ~1.9 | 1.910571 | +0.010571 |
| Preceding-fortnight matched-hour maximum | 125.5 ug/m3 | 125.5 ug/m3 | 0 |
| Territory-wide NO2 mean rise, other 17 | ~+19.6 ug/m3 | 19.564119 ug/m3 | -0.035881 |
| Tai Po PM2.5 excess over the other-17 mean | ~+7.4 ug/m3 | 7.401836 ug/m3 | +0.001836 |
| Tai Po PM2.5 evening-elevation rank | 1st of 18 | 1 of 18 | 0 |

The snapshot includes:

| Reference check | Observed |
| --- | ---: |
| Tai Po NO2 at 2025-11-26 20:00 | 123.2 ug/m3 |
| Tai Po AQHI at 2025-11-26 20:00 | 5 (MODERATE) |
| PM2.5 24 h post-fire peak | 54.6 ug/m3 |
| PM2.5 pre-fire mean | 26.675 ug/m3 |
| NO2 24 h post-fire peak | 123.2 ug/m3 |
| NO2 pre-fire mean | 48.903 ug/m3 |
| Matched-hour z (NO2, 20:00 vs 12-25 Nov) | 1.911 (paper ~1.9) |
| Preceding-fortnight matched-hour maximum | 125.5 ug/m3 (paper 125.5) |
| Territory-wide NO2 mean rise, other 17 stations | 19.564 ug/m3 (paper ~+19.6) |
| Tai Po PM2.5 excess over the other-17 mean | 7.402 ug/m3 (paper ~+7.4) |
| Tai Po PM2.5 evening-elevation rank | 1 of 18 (paper 1st) |
| Wind at 2025-11-26 20:00 | 0.389 m/s from 207 deg (SW) |
| Educational sensitive areas, 1.5 km from fire site | 45 |
| Educational sensitive areas, 1.5 km from Tai Po station | 53 |
| OSM buildings, 2 km from fire site | 4,434 |
| Residential OSM buildings, 1.5 km from fire site | 1,153 |

The last four rows are **snapshot screening counts**, not paper reproductions.
The paper's 1,090 total / 293 residential OSM counts are from a different
snapshot/filter, and the fire-site-centred school count (45) differs from the
station-centred `check_agent` truth (53). Both are kept explicit in the JSON.
The 54.6 ug/m3 value is an hourly peak inside the 24 h window, so comparing it
with the 24 h AQO (37.5 ug/m3) or WHO guideline (15 ug/m3) is a screening
comparison, not a legal compliance determination.

**Caveat.** The peak-to-mean ratio is a screening signal, not causal
attribution. The matched-hour z-score, the territory-wide network contrast and
the near-calm wind do not support a single-source plume. The reference's
`anomaly_assessment` therefore reports `raw_ratio`, `matched_hour_z`,
`matched_hour_max_prev_14d`, `network_mean_rise`,
`nearest_station_rise_vs_network`, `pm25_rank_of_nearest` and
`supports_local_plume: "false"` with the fixed rule printed in the JSON; it is a
rule-based assessment, not an LLM judgement. The paper's exact definitions are used: the matched-hour baseline is
20:00 on 26 November against 20:00 on 12-25 November (sample standard deviation,
`n=14`); the network contrast is the event evening 18:00-23:00 against the same
clock-hours on those 14 days; and the nearest-station excess is Tai Po's PM2.5
rise minus the mean rise of the other 17 stations.

`docs/tai_po_reference.json` is the committed snapshot produced by
`--out docs/tai_po_reference.json`. It has 21 checks, including the five paper
reanalysis values above. The full live run is read-only and uses no model.

`analysis/daemon.py` keeps the foreground drift monitor and adds the paper's
AQHI alert path. It creates and disposes a read-only engine per check, reports a
database error as `degraded` without crashing, and prints one JSON object per
line:

```powershell
uv run python -m analysis.daemon --once
uv run python -m analysis.daemon --interval 300
uv run python -m analysis.daemon --alerts --once
$env:REPLAY_NOW = '2025-11-26 15:30'
uv run python -m analysis.daemon --alerts --once
```

The alert path uses `check_alerts(conn, now, threshold, cooldown)`:
`AQHI_ALERT` defaults to `7` and is environment-configurable; the per-station
cooldown defaults to 3600 seconds (`AQHI_ALERT_COOLDOWN_SECONDS`); the latest
hourly AQHI at or before `now` is used; and `REPLAY_NOW` is authoritative, so a
replay never falls back to wall-clock time. The default job text is
`Assess the air quality situation near <station> and nearby sensitive receptors.`
and the callback is pluggable, so the daemon can hand the query to the real
agent later. At the requested replay `2025-11-26 15:30`, every station's latest
hour is 15:00 with AQHI 5-6, below the threshold; the correct result is **zero
triggers**. The DB test in `tests/test_tai_po_reference.py` also proves the path
does trigger on synthetic rows and respects cooldown. It writes no files and no
database rows. It is not the Day 10 execution isolation boundary.

### Agent-to-reference replication

`scripts/compare_agent_to_reference.py` is a separate, non-frozen replication
check. The live run described here asked the v3 **single** arm (no workers, no
verifier) for exactly eight fields under the exact `ANSWER` dictionary
contract, three times, and compared each field with the deterministic reference
computed from the read-only database. The evaluated prompt and the scorer were
unchanged at that time; the v4 rerun is reported separately below. The
tolerances are
0.1 for `no2_at_2000`, `pm25_post_peak`, `matched_hour_z` and
`matched_hour_max_prev_14d`; 0.2 for `network_mean_rise` and
`nearest_station_rise_vs_network`; exact integer equality for
`pm25_rank_of_nearest`; and 0.01 for `wind_speed_2000`. The two requested chart
configs are checked structurally: line chart, title containing `NO2`, 14 x
values, one series of length 14, y-axis containing `ug/m3`; and bar chart, title
containing `PM2.5`, 18 x values, one series of length 18, y-axis containing
`ug/m3`. A failure is printed verbatim and stored in the run artifact. The
single arm uses the same `_stall_guarded_llm` runtime safeguard as the
single-verified control so repeated identical code does not consume the whole
step budget; it adds no domain hint.

The live 3-repeat run on 2026-11-18 produced **0/24 field matches**, **0/3
`ANSWER` dicts**, **0/3 line charts** and **0/3 bar charts**; all three runs
completed (3/3). The deterministic reference passed 21/21 checks
(`reference_ok: true`). Every one of the eight fields was 0/3.

The failures are reported verbatim:

- Repeat 1: 3 steps, 405.6 s, 29,498 tokens. `last_error` was
  `SecurityError: forbidden call: exit()`. Final text:
  `I stopped because the identical code block was about to run again. Use the
  values already staged, change the query, or report the limitation.`
- Repeat 2: 2 steps, 98.7 s, 13,901 tokens. Same security error. Final text:
  `Tai Po station not uniquely resolved.`
- Repeat 3: 2 steps, 262.3 s, 20,162 tokens. Final text: the same stall-guard
  message as repeat 1.

The model's station query used
`lower(replace(name_en, ' ', '')) LIKE '%taipo%'` and then called `exit()` when
it did not get exactly one row; the runtime forbids `exit()`. The model never
produced the `ANSWER` dict or either chart. This is a real single-arm failure on
the multi-field contract, not a reference failure. The artifact records the
full-prompt hash `d23ae723...`; recomputing the base-only hash with the same
`PROMPT_TIME` gives `538596df...`, identical to the frozen v3 single baseline.
The difference is the merged-role addendum, so the evaluated base prompt was not
edited. The artifact is `runs/reference_compare/day8_single_3x/run.json`
(gitignored).

## v4: paired weather view, prompt affordance and deterministic reference endpoint

This pre-registered v4 supersedes the earlier freeze for
`domains/hk/schema_prompt.py` only. The cases, truth values, tolerances,
scorer (`v2`), role prompts, budget (20 steps), repeats (3) and model are
unchanged. No Day 10 work has started.

### Paired weather view

`scripts/migrate_views.py` creates or replaces the read-only view
`paired_hourly_weather` with exactly these columns: `aq_station_id`,
`aq_station_code`, `aq_station_name`, `datetime`, `temperature`, `humidity`,
`pressure`, `wind_speed`, `wind_gust`, `wind_degree`, `wind_direction`. It
grants `SELECT` to `neuro_ro`, changes no data, and is idempotent. The correct
pairing is now a relation instead of a rule the model has to reconstruct:

```sql
JOIN weather_station ws ON ws.station_code = 'wx_' || a.station_code
JOIN hourly_station_weather w ON w.station_id = ws.id
```

The migration run printed `157,680` total rows and `8,760` Tai Po rows.
`tests/test_paired_view.py` proves that Tai Po has 24 rows on 2025-11-26, that
the 20:00 row has `wind_speed = 0.389`, and that the old
`a.station_id = w.station_id` join returns `0` rows for the same window. Those
tests skip only when the database is unreachable; a reachable database without
the view is a failure.

### Prompt and hashes

`render_schema()` lists the view with the description line:

> hourly weather already paired to each AQ station; use this instead of joining hourly_station_weather by station_id

The `wx_` join rule is retained. `DOMAIN_RULES` now also says: if you cannot
resolve the question, set `ANSWER = None` and explain in the final text; never
call `exit()` or `quit()`. `validate_schema()` includes the view in its live
column assertion, and `tests/test_prompt_leakage.py` checks that the new text
does not contain a copied truth query.

The v4 hashes are:

| Artifact | SHA-256 |
| --- | --- |
| Base prompt with live data window | `ff04f301a5eebdc041b8e0e58260ab5cd4011729463f94c785e2201dacea585f` |
| Multi prompts snapshot | `141925a0538b2e039cb0d4ac78589ff0ddd3a99e3e5a27b5cd322976289c23a1` |
| Merged single-role addendum | `2c0a50e48797d11f7b2b8d416447681eb643e482ac39cc6a0117297c941e641d` |

The multi prompts and merged-role addendum are unchanged from v3; only the base
prompt changed.

### Premise-trap transcripts

`docs/premise_trap_transcripts.md` transcribes the last live rule-judged run
(`runs/premise_traps/day6_live/run.json`) without rerunning it. The strict score
remains primary: **1/10** (`halt` 1/4, `ask clarification` 0/5,
`state limitation` 0/1). The file includes each question, final text, `ANSWER`
and strict verdict, plus the caveat that the narrow rules can understate a
semantically reasonable answer. The reviewer guidance is to use the gate's
held-out `74.3%` accuracy and nine out-of-scope leaks as an advisory “may be out
of scope” warning, not a hard block; premise checking stays inside the agent.

### Deterministic reference endpoint

`GET /api/reference/tai_po` returns the committed
`docs/tai_po_reference.json` plus two deterministic ECharts configs. It reads
only that file: no LLM call and no database connection. The deterministic panel
was later removed from the visual dashboard. `api/static/index.html:296-304`
keeps a non-rendered `<template id="reference-template">` so static UI tests
still have the expected ids, and `api/static/app.js:663-664` exits
`loadReference()` when the host is inside that template. The endpoint remains
the checkable artifact; no browser page shows those precomputed values.

### v4 matched rerun results

Both arms used the v4 base prompt hash above, the frozen 20-case v2 set, scorer
`v2`, 3 repeats, a 20-step solver budget, `--resume`, and the same model. The
single arm used the unchanged merged-role addendum; the multi arm used the
unchanged multi prompts (verifier mode `overwrite`). The v3 artifacts were kept
in their original directories.

| Arm | Case-runs | Mean | v1 | v2 | Hold-out | Non-hold-out |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Single + merged roles | 60 | 78.3% (47/60) | 68.3% | 78.3% | 12/18 (66.7%) | 35/42 (83.3%) |
| Multi overwrite | 60 | 65.0% (39/60) | 63.3% | 65.0% | 10/18 (55.6%) | 29/42 (69.0%) |

The paired task-level test (`evals/compare_arms.py`, primary `v2`) gives multi
wins **3**, losses **7**, ties **10**, discordant **10**, exact McNemar
**p=0.3438**; the paired task bootstrap mean difference is **-0.133**, 95% CI
**[-0.300, +0.033]** (5000 resamples). Multi costs **2.23x** tokens, **2.34x**
seconds and **3.85x** calls per case. The multi verifier audit is
wrong→right 25, right→wrong 2, right→right 14, wrong→wrong 19, with pre-verify
16/60 and final 39/60.

The pre-registered v4 changes did **not** produce a significant multi-arm gain.
The earlier claim that cases 18 and 20 failed in all repeats was wrong:
`docs/never_solved_cases.md:64-69` records #18's single pass in multi v2 and #20's
single pass in single v4 (`1/3` in that arm); #14 also passed once in multi v4
and #15 five times. The multi run still wasted budget on malformed weather joins
(for example querying `no2_ugm3` from `paired_hourly_weather`, which only exposes
AQ identifiers and meteorology). The offline comparison artifact is
`runs/multi/day9_v4_budget20_3x/compare.json` (gitignored).

### v4 agent-to-reference replication

`scripts/compare_agent_to_reference.py` was rerun with the v4 single arm
(base prompt `ff04f301...` plus the unchanged merged-role addendum), 3 repeats,
20 steps and the same deterministic reference. The first attempt aborted before
any repeat finished with `httpx2.ReadTimeout` after a 3-minute Ollama POST; the
server was still alive. It was rerun unchanged and completed 3/3.

The successful run reports `reference ok=True`, **fields 0/24**, **ANSWER dict
0/3**, **line chart 0/3**, **bar chart 0/3**. Every one of the eight fields was
`0/3`:

| Field | Matches |
| --- | ---: |
| `no2_at_2000` | 0/3 |
| `pm25_post_peak` | 0/3 |
| `matched_hour_z` | 0/3 |
| `matched_hour_max_prev_14d` | 0/3 |
| `network_mean_rise` | 0/3 |
| `nearest_station_rise_vs_network` | 0/3 |
| `pm25_rank_of_nearest` | 0/3 |
| `wind_speed_2000` | 0/3 |

Repeat durations were 259.2 s, 372.0 s and 113.3 s. Repeats 1-2 stopped under
the identical-code stall guard; repeat 3 said “Tai Po station not uniquely
resolved.” The model again never emitted the exact `ANSWER` dictionary or the
two chart configs. This is a real single-arm failure on the multi-field
contract, not a reference failure. Artifact:
`runs/reference_compare/day9_v4_single_3x/run.json` (gitignored).

## Day 10: isolated runtime (honest scope)

The API now defaults to `RUNTIME_MODE=subprocess`; the evaluation harness still
uses the unchanged in-process `PythonRuntime`. `core/subprocess_runtime.py`
starts a Windows spawn child, checks the AST in the parent, sends code over a
Pipe, captures stdout in the child, and exposes a small namespace proxy
(`get`, `[]`, `in`, assignment, `pop`) plus an engine proxy used only to build the
system prompt. A wall-clock timeout kills the child and the next execute starts
a fresh one, so staged state is deliberately lost. Memory capping is optional
via `psutil`; no `resource.setrlimit` is used on Windows.

The child receives only `DB_URL_RO` plus the minimum OS variables; a fresh empty
temp directory is its cwd, so a relative `.env` read finds nothing. Tests prove
that a parent `DB_URL_ADMIN` and `LLM_API_KEY_STRONG` are not visible, that two
children do not mix stdout, that AST rejection happens before any RPC, and that
a live read-only role can SELECT but cannot INSERT once `25006` is lifted
(`test_child_can_read_database_and_cannot_insert`).

This is **not** a container sandbox. The child runs as the same OS user, keeps
network access to Postgres and to the geocoder endpoints, and the AST checker is
a guard rail rather than a security boundary. The real data boundary is the
`neuro_ro` grant. The in-process poison guard remains for `RUNTIME_MODE=inproc`;
subprocess mode uses `MAX_CONCURRENT_RUNS` (default 2) and never poisons the app
because the timeout kills the child. Verified by
`test_infinite_loop_is_killed_and_the_api_stays_usable` and 237 passing tests
with the database running.

## Day 10: Docker and moving the data

`Dockerfile` builds a non-root `python:3.12-slim` image with `uv` and installs
dependencies from `uv.lock`. The repo `.env` and `~/.neuro-air/admin.env` are
excluded by `.dockerignore`; neither is baked into the image. `docker-compose.yml`
adds the `api` service with healthchecks, `extra_hosts: host.docker.internal`,
and `LLM_BASE_URL` defaulting to `http://host.docker.internal:11434/v1` for a
host Ollama. The container DSN must be a separate `DB_URL_RO_DOCKER` in `.env`
with the hostname `db`.

```powershell
# add to .env: DB_URL_RO_DOCKER=postgresql+psycopg2://neuro_ro:<pw>@db:5432/airdb
docker compose --env-file .env --env-file "$HOME\.neuro-air\admin.env" up --build -d
docker compose --env-file .env --env-file "$HOME\.neuro-air\admin.env" ps
```

On a fresh machine, restore a dump instead of re-ingesting:

```powershell
uv run python scripts/dump_db.py                       # on the source machine
uv run python scripts/restore_db.py backups/<file>.dump --yes
uv run python scripts/migrate_views.py                 # idempotent paired view
```

The dump/restore scripts read `DB_URL_ADMIN` from `$NEURO_ADMIN_ENV` or
`~/.neuro-air/admin.env`; they never write credentials into the repo. Restore
is destructive and was not run in this session. The image build was run and
succeeded (`docker_build_exit=0`, image `neuroair-api:day10`); both compose
services reached `healthy`, and `GET /api/health` reported
`"runtime_mode":"subprocess","max_concurrent_runs":2`.

## Day 10: audit and final report

`docs/never_solved_cases.md` is the read-only audit of all v2/v3/v4 runs. There
were **no** all-zero cases; #14, #18 and #20 each passed exactly once in 18
slots, and #15 passed 5 times. The four suspected cases are analysed there with
question, truth SQL, truth value, a representative failure and the judgement
that the truth SQL implements the single defensible reading.

`docs/FINAL_REPORT.md` is the plain-language final write-up, including the v4,
v5 DEV, v5 TEST and HK-12 evidence, what was not reproduced, the blocked
strong-model preflight and the exact unverified items.

Strong-model preflight on 2026-11-22 added `scripts/watch_run.py` and
`tests/test_watch_run.py`; the watchdog tests passed `8 passed in 0.10s` on the
Windows host. `.env` was set to `LLM_MODEL_STRONG=deepseek-flash` and frozen
truth validation passed `20/20`. Two DeepSeek smoke calls then returned `402
Insufficient Balance`, so the pre-agreed abort rule stopped the sequence before
the mini run, single arm or multi arm. No strong score, no billed amount and no
strong/local comparison were produced. DeepSeek thinking remains at the
provider default (ON), so any future strong run would not be directly
comparable to the local non-thinking `qwen2.5-coder:14b` runs.

## Day 11: frozen v5 evaluation and external HK-12 anchor

The v5 benchmark is frozen at 252 tasks (53 DEV, 199 TEST). `core/*`,
`domains/hk/*`, `evals/v5/*`, `evals/run_v5.py` and the split definition were
not edited. The final evidence chain is:

- `docs/V5_DIAGNOSIS.md`: post-fix DEV diagnosis, single `49/53 (92.5%)` vs
  multi `42/53 (79.2%)`, `−13.2 pp`, `p=0.0654`. The original Gate A failed on
  22 stalls; it was amended to integrity-only afterwards and the original text
  is retained in `docs/V5_PLAN.md`.
- `docs/V5_TEST_REPORT.md`: one TEST run per arm. Single `182/199 (91.5%)` vs
  multi `166/199 (83.4%)`, `−8.0 pp`, 11 wins / 27 losses / 161 ties, exact
  McNemar `p=0.0139`, bootstrap 95% CI `[−14.1, −2.0] pp`. Single exceeded the
  pre-declared 90% ceiling, so TEST cannot show gain; the significant negative
  result is harm. Multi used `4.89×` median tokens and `5.36×` summed wall
  clock. Hard tier was fixed by task type in `evals/v5/tasks.py` before TEST.
- `docs/PAPER_HK12_ANCHOR.md`: public paper fixtures at commit
  `dbc47f0c9e2192d204a04e848b253953eb1afd72`, vendored byte-for-byte with a
  validator-only compatibility shim. The first single run, `11/36 (30.6%)`,
  was inflated by a paper-ID/local-ID mismatch. A question-text-only identifier
  crosswalk corrected the single score to `14/36 (38.9%)` (ID-bearing tasks
  `0/18 → 4/18`); paper matrix is `119/120` cells. Paired multi was not rerun
  and remains skipped at the ~2-hour cap (10/36 case files, no `run.json`), so
  no HK-12 paired claim is made.
- `docs/results/v5_test_cases.csv`, `docs/results/v5_dev_cases.csv` and
  `docs/EVIDENCE_MANIFEST.md` make the frozen v5 and HK-12 run evidence
  checkable without the gitignored `runs/` tree. The CSVs are generated by
  `scripts/export_case_csv.py`; `tests/test_evidence_csv.py` re-derives the
  TEST headline numbers from the TEST CSV.
- The paper's `82.0% → 88.4%` (`+6.4 pp`, `p=2.5×10⁻⁵`) was **not**
  reproduced. Only local `qwen2.5-coder:14b` was used. The `*_STRONG` keys are
  set but were not used or printed.

Final suites on the current tree: Windows host `323 passed, 1 skipped in
30.33s`; rebuilt `api` container `323 passed, 1 skipped in 25.31s`. The stale
image first reported `238 passed, 1 skipped` because it predated the new tests;
the image was rebuilt and the suite rerun.

## Conventions

- The agent only ever connects with the read-only `neuro_ro` role. Ingestion
  and `truth_sql` use `DB_URL_ADMIN`.
- The repo `.env` holds only `DB_URL_RO` and `LLM_*`; the admin DSN and
  `POSTGRES_*` live in `~/.neuro-air/admin.env` (override `NEURO_ADMIN_ENV`).
  `admin_url()` returns the DSN to the caller and never exports it
  (`core/env.py`).
- The LLM writes Python; the numbers come from the runtime. No arithmetic in
  the model's head.
- `YYYY-MM-DD to YYYY-MM-DD` includes both dates in full (end is exclusive at
  the next midnight).
- Distances are in metres because location columns are `geography`; casting to
  `::geometry` silently switches to degrees.
- The Day 4 schema prompt must only describe tables that actually hold rows.
  A query against an empty table returns a silent `0`, which is worse than an
  error; `eia_*`, `traffic_camera`, `land_use_zone` and `building_height_control`
  stay out until Day 2 populates them.

## Troubleshooting

- `docker: command not found` in a terminal that was already open before the
  install: close and reopen it. The installer appends
  `C:\Program Files\Docker\Docker\resources\bin` to the user `PATH`.
- `error getting credentials ... docker-credential-desktop` means the same
  stale-`PATH` problem.
- Ollama answers on `:11434` only after the desktop app (or `ollama serve`) is
  running; start it once per reboot if it is not set to launch at login.
- `admin env ... not found` or `POSTGRES_PASSWORD ... set POSTGRES_PASSWORD`: the
  admin file is missing or compose was run without it. Create
  `~/.neuro-air/admin.env` from `admin.env.example`; `scripts/dev_up.ps1`
  passes it automatically, and a manual compose command needs
  `--env-file "$HOME\.neuro-air\admin.env"`.
- `admin DSN ...` from a test means the script loaded the agent environment
  first. Call `admin_url()` before `load_agent_env()`, as
  `scripts/check_agent.py` and `scripts/check_db.py` do.

## Status

| Day | Deliverable | State |
| --- | --- | --- |
| 0 | scaffold, deps, compose, env check | done |
| 1 | PostGIS schema, read-only role | done |
| 2 | HK data ingestion | done |
| 3 | LLM client, runtime, security, single agent | done |
| 4 | schema prompt, geocoder, charts, frozen v2 baseline | done |
| 5 | coordinator, workers, verifier | implemented; official 3-repeat 41/60 vs single 46/60 (p=0.109; 5.21x tokens) - no gain |
| 5b | matched-budget multi audit, verifier modes, v3 prompt | v3 rerun done: 40/60 multi vs 45/60 single (p=0.289) - negative; flag 12/42 complete; single-verified 35/42 vs single 34/42 (p=1.000) - not significant; none incomplete (21/42); strong API later configured; smoke blocked by DeepSeek 402 Insufficient Balance (2026-11-22) |
| 6 | relevance gate, knowledge retrieval, verifier tuning | implemented; live gate calibration theta=0.54, held-out 0.743; live premise traps 1/10 (diagnostic, not a benchmark); strict transcripts in `docs/premise_trap_transcripts.md` |
| 7 | FastAPI SSE API and ECharts demo UI | implemented; 224 tests pass; timeout-poison 503 guard added; live demo 5/5 HTTP 200: single 4/4 answered, but the multi Tai Po run joined weather on the wrong key, returned no data and the verifier confirmed that wrong answer |
| 8 | deterministic Tai Po reference, daemon | implemented; reference 21/21 checks, paper targets reproduced, daemon replay 0 triggers; live v3 agent-to-reference 0/24 fields, 0/3 ANSWER dicts, 0/3 charts - single arm failed, reference valid |
| 9 | v4 paired view, prompt affordance, matched rerun, reference UI | done; paired view migration + regression tests, base hash `ff04f301...`, single 47/60 vs multi 39/60 (McNemar p=0.3438), deterministic `/api/reference/tai_po` endpoint; visual reference removed from UI and kept as a non-rendered template; strong-model evaluation attempted, blocked by DeepSeek 402 Insufficient Balance (no charge or run) |
| 10 | never-solved audit, subprocess runtime, Docker packaging, final write-up | done; `docs/never_solved_cases.md`, spawn-backed API runtime, `Dockerfile`/compose API service, `docs/FINAL_REPORT.md`; 238 passed, 1 skipped with DB up; after watchdog 239 passed, 8 skipped with DB stopped; strong-model run attempted, blocked by 402 |
| 11 | frozen v5 DEV diagnosis, single TEST split, HK-12 external anchor, evidence archive, final docs | done; v5 TEST single 182/199 vs multi 166/199 (`−8.0 pp`, `p=0.0139`, ceiling >90% so harm not gain); HK-12 raw 11/36 was an ID artifact, corrected crosswalk single 14/36, paper 119/120, paired multi skipped at ~2h cap; CSV/manifest evidence committed; strong keys set but unused; final host and rebuilt-container suites both 323 passed, 1 skipped |
