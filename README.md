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
2. **CO nulls at 7 stations:** Only 11 of the 18 stations report CO measurements. The remaining 7 stations report NULL (63,474 NULL values overall).
3. **Occasional pollutant nulls:** Periodic instrument calibration or maintenance results in nulls (e.g., 5,398 PM2.5 nulls and 4,428 NO2 nulls across 157k rows).
4. **HKO wind nulls & calm hours:** 3,558 nulls occur where HKO station feeds skipped snapshots. When wind speed is 0 (calm), wind direction/degree is recorded as NULL.

### Deviations from the Implementation Plan

1. **Pollutants come from the archived XML, not the live feed:** The live feed only serves the last 24 hours, so a historical 2025 ingest requires data.gov.hk's monthly archive bundles.
2. **CO is not multiplied by 1000:** The published data dictionary confirms all pollutants are reported in ug/m3. Scaling CO would produce an unphysical 600 mg/m3 instead of the real 0.5-0.9 mg/m3.
3. **Temperature, humidity, and pressure from ERA5:** HKO Open Data does not expose an hourly historical temperature/humidity API (only astronomical tides). ERA5 fills these columns, while wind uses real HKO weather station observations.
4. **Curated station coordinates (~300 m precision):** Taken from official EPD station maps.
5. **Weather station mapping:** Each AQ station is paired with its nearest HKO automatic weather station via `hko_name` in `domains/hk/stations.py`.
6. **HKO wind compass conversion:** HKO reports 16 compass directions rather than degrees. Vector means are computed and converted to degrees, and `wind_direction` is mapped to the schema's 8-point enum.
7. **OpenStreetMap POIs & Tai Po AOI buildings:** Full Geofabrik 137k-building PBF is heavily throttled/unreachable on this network. Overpass loads all Hong Kong schools, hospitals, clinics, places of worship, parks, and industrial areas, plus building centroids within 2.5 km of Tai Po (Wang Fuk Court, 22.4468 N, 114.1754 E) for the Day 8 fire case study.
8. **Sensitive area restrictions:** `HEALTH_CARE_INSTITUTION` in `sensitive_area` is strictly restricted to `amenity=hospital` (clinics, doctors, dentists are kept only in `osm_feature`).
9. **Empty tables reserved for later days:** `street`, `road_segment`, `hourly_road_segment_traffic`, `eia_project`, `eia_habitat`, `traffic_camera`, `land_use_zone`, and `building_height_control` remain empty until their respective implementation days.

### Notes for Day 4 (Schema Prompt & Baseline)

- **Only populated tables in prompt:** The Day 4 schema prompt must only expose the 6 populated tables (`air_quality_station`, `hourly_station_air_quality`, `weather_station`, `hourly_station_weather`, `osm_feature`, `sensitive_area`). Prompting on empty tables causes the LLM to write queries that return 0 rows.
- **Sensitive receptors:** Schools and hospitals must be queried from `sensitive_area` (`EDUCATIONAL_INSTITUTION`, `HEALTH_CARE_INSTITUTION`).
- **Spatial queries & geography cast:** `osm_feature.geometry` and `sensitive_area.geometry` are `geometry(4326)`. To measure distances in metres rather than degrees, the model must cast geometries to `::geography` (e.g. `ST_DWithin(geometry::geography, ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography, distance_in_metres)`).

### Sanity Values

The plan's Tai Po case study checks reproduce:

| Metric | Target | Ingested DB | Status |
| --- | --- | --- | --- |
| Tai Po NO2 (2025-11-26 20:00) | 123.2 ug/m3 | 123.2 ug/m3 | exact match |
| Tai Po AQHI (2025-11-26 20:00) | 5 (MODERATE) | 5 (MODERATE) | exact match |
| Tai Po PM2.5 Peak (2025) | 54.6 ug/m3 (case-study) | 84.2 ug/m3 (full-year peak) | expected |
| Tai Po Wind (2025-11-26 20:00) | - | 0.389 m/s from 207° (SW) | real HKO observation |

## Conventions

- The agent only ever connects with the read-only `neuro_ro` role. Ingestion
  and `truth_sql` use `DB_URL_ADMIN`.
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
| 3 | LLM client, runtime, security, single agent | next |
