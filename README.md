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
api/                  FastAPI app, SSE stream, monitoring daemon, static UI
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
```

The runner records the model, the prompt sha256, the pinned and recomputed truth
values, per-case steps/tokens/seconds, first-attempt status, code blocks and
outputs in `runs/baseline/<timestamp>/run.json`, plus the exact prompt in
`prompt.txt`. A truth mismatch stops the run instead of silently moving the
benchmark. `--arm single` is the only arm until Day 9; `--no-holdout` is for
prompt work and must not be used for the frozen reference.

#### Frozen v2 result (strict scorer)

Artifact: `runs/baseline/20261110_191651/run.json` (local, gitignored).
Model: `qwen2.5-coder:14b`; prompt sha256
`ab5ef7e968ba542a367164e01fd54470672d48ec4556ac9f0ef96627b61b3048`;
three repeats, **max 10 steps**. This is the Day 4 reference, not the Day 9
matched-budget baseline: the plan requires **20 total steps for both the single
and multi arms**. The runner now defaults to 20; rerun v2 at 20 before Day 9, or
compare both arms at 10. Do not compare this 10-step table with a 20-step
multi-agent arm.

| Repeat | Passed | Hold-out | Non-hold-out | First attempt | Mean steps | Tokens | Wall-clock |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 16/20 | 4/6 | 12/14 | 18/20 | 3.15 | 266,960 | 455.2s |
| 2 | 15/20 | 4/6 | 11/14 | 18/20 | 2.65 | 184,983 | 252.6s |
| 3 | 13/20 | 3/6 | 10/14 | 20/20 | 2.60 | 179,163 | 213.2s |
| **Mean** | **73.3%** | **61.1%** | **78.6%** | **56/60** | **2.80** | **631,106** | **15.4s/case** |

| # | Category | Difficulty | Hold-out | Pass / 3 |
| ---: | --- | --- | :---: | ---: |
| 1 | retrieval | simple | no | 3/3 |
| 2 | retrieval | simple | yes | 3/3 |
| 3 | retrieval | medium | no | 2/3 |
| 4 | retrieval | simple | no | 3/3 |
| 5 | temporal | medium | no | 2/3 |
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
| 16 | statistics | medium | no | 2/3 |
| 17 | multi_table | hard | yes | 2/3 |
| 18 | multi_table | hard | no | 0/3 |
| 19 | multi_table | hard | no | 3/3 |
| 20 | multi_table | hard | yes | 0/3 |

By category: retrieval 91.7%, temporal 91.7%, spatial 100.0%, statistics
41.7%, multi_table 41.7%. By difficulty: simple 100.0%, medium 80.0%, hard
44.4%. The four repeat-zero cases are `#14`, `#15`, `#18`, `#20`; all are
multi-table or statistics questions that need the paired weather station.
`docs/failure_log.md` has the code-level evidence.

The strict scorer's two text false negatives are worth separating from model
errors: `#3` in repeat 2 printed "CO ... is not available" in the execution
output but finalized as "The question cannot be answered"; `#16` in repeat 3
printed "No PM2.5 reading available". Both are semantically correct NULL
answers, but the frozen `UNAVAILABLE` matcher does not accept those exact
phrases, so the artifact records 44/60. A semantic audit would count them as
two more passes (46/60, 76.7%); I did not change the scorer after the freeze.
The failure log records both readings.

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
| 5 | coordinator, workers, verifier | next |
