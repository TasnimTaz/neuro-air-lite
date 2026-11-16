# Data Provenance

## Station pairing

Air-quality stations and weather stations are separate tables. The weather
station code follows the air-quality station code with a `wx_` prefix. A query
that combines hourly air quality with hourly weather must join through both
station tables; joining the two hourly tables directly on `station_id` compares
two unrelated identifier spaces.

## Weather sources

Temperature, humidity and pressure come from the ERA5 reanalysis archive. Wind
speed, gust, degree and direction come from Hong Kong Observatory ten-minute
observations folded to hourly values. Because ERA5 is a reanalysis grid, its
temperature varies relatively little across Hong Kong stations. Wind is a local
observation and can differ sharply between stations, especially in complex
terrain.

## Spatial layers

Air-quality and weather station coordinates use WGS84 geography columns. The
OpenStreetMap feature layer is general map data, while `sensitive_area` is the
authoritative inventory for schools, hospitals and places of worship. Distance
questions should cast geometry to `geography` so the result is in metres; a
`geometry` cast returns degrees. The OpenStreetMap extract is a 2026 snapshot,
not a historical 2025 snapshot.

## Coverage and gaps

The ingested hourly data covers calendar year 2025. A date in 2026 is outside the
coverage window and must be reported as unanswerable rather than extrapolated.
The pollutant archive has a small number of missing station-hours. The wind
source also has occasional missing snapshots, and calm hours have no direction.
Treat those cases as missing measurements and say so explicitly.
