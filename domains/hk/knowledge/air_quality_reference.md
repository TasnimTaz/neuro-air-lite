# Air Quality Health Index

## AQHI bands

The Air Quality Health Index is reported on a scale from 1 to 10, plus a top
band published as `10+`. The database stores `10+` as the integer 11 and stores
the authoritative label in `aqhi_level` as `SERIOUS`. The remaining bands are
`LOW`, `MODERATE`, `HIGH` and `VERY HIGH`. A high AQHI is a health-risk band, not
a pollutant concentration, so it is not interchangeable with PM2.5 or NO2.

## Air Quality Objectives

Hong Kong has two Air Quality Objective regimes in the 2025 data. The earlier
regime applies from 2022-01-01 through 2025-04-10. The current regime applies
from 2025-04-11 onward. The regime matters when a question asks whether a
concentration exceeded an objective, because the same pollutant can have a
different limit on either side of the change date. The knowledge base records
the source URL for each regime; a query should state which regime it used.

## Units and pollutants

AQHI is unitless. PM2.5, PM10, ozone, sulphur dioxide, nitrogen dioxide and
carbon monoxide are stored in micrograms per cubic metre (`ug/m3`). Carbon
monoxide is not multiplied by 1000: the published source already uses `ug/m3`,
and a scaled value would be unphysical. Temperature is in degrees Celsius,
pressure in hectopascals, wind speed and gust in metres per second, and wind
direction in degrees clockwise from north.

## Missing measurements

NULL means the measurement is absent, not zero. Carbon monoxide is NULL at seven
of the eighteen air-quality stations because those stations do not report it.
Wind degree and wind direction are NULL in calm hours. Some station-hours are
missing from the upstream pollutant archive. A "not available" answer is
preferable to silently substituting zero, and an empty result should be reported
as empty rather than as a numeric zero.

## Health and guideline context

The AQHI is a local short-term health index. The World Health Organization 2021
global air-quality guidelines are long-term or annual reference values and are
not the same as the Hong Kong AQOs. Do not compare a WHO guideline directly with
an AQHI band. When a question asks for a health interpretation, state which
reference framework is being used and cite the corresponding value.
