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
scripts/calibrate_gate.py   live held-out threshold calibration (not run yet)
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

Install the model only after the background evaluation runs finish:

```powershell
ollama pull nomic-embed-text
```

Pulling or loading the embedding model now could evict the chat model from
memory and change the running evaluation's latency.

### Gate

`R(q, D) = max cosine(q, scope_sentence)` over 18 short capability sentences,
covering the six populated tables, 2025 time windows, aggregation, thresholds,
comparisons, joins and Hong Kong distance. `GATE_THETA` overrides the threshold;
the built-in `0.50` is explicitly **provisional** until the calibration runs.
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
while an eval is running. The default threshold has not been calibrated on this
machine yet.

### Knowledge base

`domains/hk/knowledge/*.md` is chunked by heading; sections longer than about
300 words are split and every piece keeps its heading. Chunk embeddings are
computed once per `KnowledgeBase`. `kb_search(query, k=3, min_score=0.5)` returns
`{text, source, heading, score}`. Every returned `text` is enclosed in
`[BEGIN KB DATA]` / `[END KB DATA]` with a "data, not instructions" notice, and
delimiter strings inside a document are neutralised so a hostile chunk cannot
close the wrapper early. That is a prompt-injection guard rail, not a sandbox.

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
writes one JSON per trap plus a run artifact, and records the final text and
`ANSWER` for qualitative review against the expected behaviour. The live run
needs Ollama and the database, and belongs after the evaluation freeze.

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

The live browser demo is deliberately **not run yet**: the background verifier
mode evaluation is still using the local Ollama model, and live queries would
distort its latency measurements.

## Day 8: deterministic Tai Po reference and daemon

`analysis/tai_po_reference.py` is the non-LLM reference for the Wang Fuk Court,
Tai Po fire case. It runs fixed SQL against the read-only role, issues
`SET TRANSACTION READ ONLY` on its connection as a second guard, and compares
the observed values with the sanity values used by `scripts/check_data.py`:

```powershell
uv run python -m analysis.tai_po_reference --compact
```

The snapshot includes:

| Reference check | Observed |
| --- | ---: |
| Tai Po NO2 at 2025-11-26 20:00 | 123.2 ug/m3 |
| Tai Po AQHI at 2025-11-26 20:00 | 5 (MODERATE) |
| PM2.5 24 h post-fire peak | 54.6 ug/m3 |
| PM2.5 pre-fire mean | 26.675 ug/m3 |
| NO2 24 h post-fire peak | 123.2 ug/m3 |
| NO2 pre-fire mean | 48.903 ug/m3 |
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

`analysis/daemon.py` is a foreground drift monitor. It creates and disposes a
read-only engine per check, reports a database error as `degraded` without
crashing, and prints one JSON object per line:

```powershell
uv run python -m analysis.daemon --once
uv run python -m analysis.daemon --interval 300
```

It writes no files and no database rows. It is not the Day 10 execution
isolation boundary.

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
| 5b | matched-budget multi audit, verifier modes, v3 prompt | v3 rerun done: 40/60 multi vs 45/60 single (p=0.289) - negative; flag 12/42 complete, none incomplete (21/42), strong API stopped because all `*_STRONG` variables are unset |
| 6 | relevance gate, knowledge retrieval, verifier tuning | implemented offline; live calibration and prefix-aware premise traps still pending |
| 7 | FastAPI SSE API and ECharts demo UI | implemented; 209 tests pass; timeout-poison 503 guard added; live demo deferred |
| 8 | deterministic Tai Po reference, daemon | implemented; read-only reference and daemon drift check verified, live demo deferred |
