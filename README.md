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
.\scripts\dev_up.ps1           # starts Docker Desktop if needed, waits for healthy
docker compose ps
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
eval/                 PV benchmark tasks, validators, analyze
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
and `truth_sql` use `DB_URL_ADMIN`; the agent never sees that URL. `domains/hk/db.py`
is the only sanctioned way to build either engine (`make_ro_engine()` and
`make_admin_engine()`); the read-only profile pins `pool_size=5`,
`max_overflow=10`, `pool_pre_ping=True`, `connect_timeout=5` and passes
`statement_timeout` as a startup option so a changed role default cannot lift it.

The Postgres superuser password comes from `POSTGRES_PASSWORD` in `.env`
(`docker compose` reads that file automatically). It only applies when the
`pgdata` volume is first created; changing it later needs `ALTER USER` or
`docker compose down -v`.

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
read-only database, and answers from the printed output. It is the baseline the
Coordinator/worker hierarchy is measured against on Day 9.

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
2. The admin DSN is never exported. `core.env.load_agent_env()` copies only
   `DB_URL_RO` and `LLM_*` from `.env` into `os.environ` and scrubs
   `DB_URL_ADMIN`/`POSTGRES_*`; `admin_url()` hands the DSN to `truth_sql` for
   this script only. Day 10 replaces this with a subprocess and a scrubbed env.

Ingest scripts keep using `ingest.common.load_env()`, which exports everything.
Only the agent-side entry points (`build_runtime()`, `scripts/check_agent.py`,
`tests/`) use the restricted loader.

### Prompt

The schema description is curated, not introspected: introspection only reports
enum *type* names, not labels, and loses units and provenance. Every table,
column, enum label, unit and source (ERA5 for temperature/humidity/pressure, HKO
for wind) is written out. `validate_schema()` compares that description with
`information_schema` at prompt-build time and raises if a column is missing or a
new one appeared undocumented, so the prompt cannot silently drift.

The prompt also injects the real coverage window and the 18-station inventory
(name, code, type) read from the database, uses `Asia/Hong_Kong` for "now"
(Windows needs `tzdata`, added to the dependencies), and states the NULL rules:
CO is NULL at 7 of 18 stations, `wind_degree`/`wind_direction` are NULL in calm
hours, and NULL means "no measurement", never 0.

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

**Baseline result** (`qwen2.5-coder:14b`, local Ollama, 62 tests green):

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
2. **`DB_URL_ADMIN` is not in the agent process environment.** The plan's
   snippet calls `load_dotenv()`, which exports every key; a module that already
   holds `os` (pandas does) could then read the admin password from model code.
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
host-side geocoder; and a host-side chart builder. The acceptance gate is a
15-question baseline plus two live tool probes.

```
domains/hk/schema_prompt.py  curated DATABASE_SCHEMA, KNOWLEDGE_BASE, EXAMPLES
domains/hk/tools.py          geocode(): Nominatim -> Lands Department ALS fallback
domains/hk/viz.py            EChartsConfigManager.line/bar/to_json/save_html
evals/hk_baseline.py         15 cases, truth SQL, pinned values, ANSWER hint
scripts/check_day4.py        probes + 15-query baseline + runs/day4_baseline.json
```

### Prompt

- `core/prompts.py` now holds only the domain-neutral part: role, injected
  variables, the SQLAlchemy bind-parameter rule, the date convention and the
  read-only rule. `domains/hk/schema_prompt.py` appends HK rules, the knowledge
  base, four worked examples and the curated schema.
- The prompt is assembled by concatenation, not `str.format`; JSON braces are
  literal. This removes the Day 3 `KeyError: '"station"'` failure mode.
- `KNOWLEDGE_BASE` carries AQHI bands (`10+` = SERIOUS), both AQO regimes
  (2022-01-01..2025-04-10 and from 2025-04-11), WHO 2021 guidelines, the
  ug/m3 unit rule (CO is not multiplied by 1000), and a source URL for each.
- `validate_schema()` still asserts the curated description against
  `information_schema` at prompt build time; the dynamic data-window block
  lists the real 2025 range, all 18 stations and their `station_type`.
- Measured size: 12,267 characters, about 3,066 tokens. Ollama's loaded context
  is confirmed at 16,384 via `/api/ps`, leaving room for the conversation.

### AQHI "10+" repair

EPD publishes the top band as `10+`. The first ingest parsed it as 10, so 47
serious station-hours were labelled VERY HIGH. `ingest/aq.py` now maps `10+` to
`aqhi = 11`; `aqhi_level = 'SERIOUS'` is the authoritative label. Re-ingesting
the 12 monthly CSVs changed 47 rows (all April 2025), left the total at 157,536,
and `scripts/check_data.py` asserts that `aqhi > 10` and `SERIOUS` agree. This is
why baseline case 3 expects 47.

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

### Baseline result

`uv run python scripts/check_day4.py` runs the static probes, the 15 questions
and two live probes (chart, geocode). Ground truth is recomputed from the
database and compared with the pinned values in `evals/hk_baseline.py`.

| Metric | Result |
| --- | --- |
| Baseline (final run) | 15/15 |
| Baseline (first full run) | 13/15 |
| First-attempt correct | 13/15 |
| Mean steps | 2.1 |
| Mean time per question | 14.3 s |
| Tokens | 144,642 |
| Categories | retrieval 3/3, temporal 3/3, spatial 3/3, statistics 3/3, comparison 3/3 |
| Live probes | chart PASS, geocode PASS |
| Gate | >= 10/15 + all probes: **pass** |

The two first-run misses are kept rather than prompt-tuned away, following the
Day 3 precedent:

1. **#1 latest-day PM2.5** used `datetime >= (SELECT max(datetime) ...)` instead
   of flooring to the date, so it selected a single hour whose Tai Po PM2.5 was
   NULL and reported "no data" instead of retrying. The second run floored to
   the day and passed.
2. **#5 weekday/weekend** used PostgreSQL `extract(dow) < 5`, which treats Sunday
   as a weekday and Friday as a weekend (`dow`: Sun=0..Sat=6), giving -0.32
   instead of -5.64. The second run used the correct classification and passed.

Both are real model failure modes the Day 5 verifier and the Day 9 comparison
should catch, not prompt defects to hide. The rerun's 15/15 shows the task is
solvable with the same prompt; the spread 13-15 is reported honestly.

### Day 4 deviations and limits

1. **Curated schema, not raw DDL.** Kept from Day 3; `validate_schema()` is the
   guard. The rest of the plan's prompt skeleton is followed.
2. **`geocode()` returns an extra `score`.** Needed because ALS is fuzzy; without
   it a caller cannot distinguish a building match from a junk match.
3. **AQHI is encoded as 11 for EPD's `10+`.** The column is an integer and the
   enum has SERIOUS; recomputing AQHI from pollutants is explicitly forbidden
   in the prompt.
4. **Baseline is a Python module plus a JSON artifact**, not a notebook: the
   values are pinned, the truth SQL is re-run, and `runs/day4_baseline.json`
   carries per-case steps, tokens, last code and last error.
5. **The two live probes are not part of the 15.** They check tool use (did the
   model call `geocode`, did it build a chart with 30 x-points), which is a
   different question from numeric accuracy.

## Conventions

- The agent only ever connects with the read-only `neuro_ro` role. Ingestion
  and `truth_sql` use `DB_URL_ADMIN`.
- Agent processes load only `DB_URL_RO` and `LLM_*` from `.env`; the admin DSN
  is passed to `truth_sql` directly and never exported (`core/env.py`).
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

## Status

| Day | Deliverable | State |
| --- | --- | --- |
| 0 | scaffold, deps, compose, env check | done |
| 1 | PostGIS schema, read-only role | done |
| 2 | HK data ingestion | done |
| 3 | LLM client, runtime, security, single agent | done |
| 4 | schema prompt, geocoder, charts, 15-query baseline | done |
| 5 | verifier agent | next |
