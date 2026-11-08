-- Hong Kong air quality database schema for neuro-air-lite.
--
-- Source: DATABASE_SCHEMA in the reference implementation
-- (Neuro-Air/benchmark/domains/HK/prompts.py), which is a prompt string rather
-- than runnable SQL. This file is the runnable version. Fixes applied:
--
--   1. `-- square meter,` style comments that had swallowed the rest of the line
--      in sensitive_area / land_use_zone / building_height_control were moved
--      onto their own lines.
--   2. `--` comments inside the ENUM value lists of land_category and
--      building_height_control_type were removed (SQL does not allow them).
--   3. `road_segment.direction` referenced the type `road_segment_direction`,
--      which is never defined upstream. It is defined here.
--   4. eia_habitat was declared before eia_project although it has a foreign key
--      to it; the creation order is fixed.
--   5. A GIST index on osm_feature.geometry was added (the table holds the whole
--      HK OSM extract and every spatial query would otherwise be a seq scan).
--   6. air_quality_station.station_code was added as the natural key the monthly
--      ingest upserts on. Without it, re-running ingest duplicates stations.
--
-- Everything else - names, types, columns, constraints, comments - follows the
-- original. Timestamps are naive `timestamp` values in Hong Kong local time
-- (the container runs with TZ=Asia/Hong_Kong).

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- Enumerated types
-- ---------------------------------------------------------------------------

CREATE TYPE air_quality_station_type_enum AS ENUM ('GENERAL', 'ROADSIDE');
CREATE TYPE air_quality_station_area_type_enum AS ENUM ('URBAN', 'RURAL', 'NEW_TOWN');
CREATE TYPE pm_ar_source_enum AS ENUM ('PM10', 'PM2_5');

-- Maps AQHI into 5 risk levels: LOW (1-3), MODERATE (4-6), HIGH (7),
-- VERY HIGH (8-10) and SERIOUS (>10).
CREATE TYPE air_quality_health_index_level_enum AS ENUM ('LOW', 'MODERATE', 'HIGH', 'VERY HIGH', 'SERIOUS');

CREATE TYPE street_type AS ENUM ('secondary', 'primary', 'highway', 'track', 'tunnel', 'unknown', 'restricted');

-- Not present in the upstream DDL even though road_segment.direction uses it.
CREATE TYPE road_segment_direction AS ENUM ('FORWARD', 'BACKWARD', 'BOTH', 'UNKNOWN');

CREATE TYPE weather_station_type_enum AS ENUM ('MANNED_WEATHER_STATION', 'AUTOMATIC_WEATHER_STATION');

CREATE TYPE wind_direction_enum AS ENUM ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW');

CREATE TYPE geometry_type_enum AS ENUM ('LINESTRING', 'POLYGON', 'POINT', 'MULTIPOLYGON');

CREATE TYPE osm_feature_main_type_enum AS ENUM (
    'BUILDING', 'TRANSPORT', 'ROAD', 'NATURAL', 'COMMERCIAL', 'PUBLIC',
    'RELIGIOUS', 'LEISURE', 'PLACE', 'INDUSTRIAL', 'HISTORIC', 'AGRICULTURAL',
    'CEMETERY', 'WATER', 'SERVICE', 'ACCOMMODATION', 'GENERAL'
);

CREATE TYPE district_enum AS ENUM (
    'Islands', 'Kwai Tsing', 'North', 'Sai Kung', 'Sha Tin', 'Tai Po',
    'Tsuen Wan', 'Tuen Mun', 'Kowloon City', 'Kwun Tong', 'Sham Shui Po',
    'Wong Tai Sin', 'Yau Tsim Mong', 'Central and Western', 'Eastern',
    'Southern', 'Wan Chai', 'Yuen Long'
);

CREATE TYPE eia_habitat_type_enum AS ENUM (
    'AGRICULTURAL_LAND', 'DEVELOPED_AREA', 'GRASSLAND', 'GRASSLAND_SHRUBLAND',
    'MARSH', 'PLANTATION', 'POND', 'SEASONAL_WATERCOURSE', 'SHRUBLAND',
    'SPECIAL_SCIENTIFIC_SITE', 'UPLAND_GRASSLAND', 'VILLAGE_AREA', 'WASTELAND',
    'WATERCOURSE', 'WETLAND', 'WOODLAND'
);

CREATE TYPE eia_application_status_enum AS ENUM (
    'APPROVED_UNCONDITIONAL', 'APPROVED_CONDITIONAL', 'PROCESSING',
    'QUASHED_FROM_CONDITIONAL'
);

-- Sensitive areas: protected or regulated zones requiring special
-- consideration in planning.
CREATE TYPE sensitive_area_category_enum AS ENUM (
    'CONSERVATION_AREA', 'COASTAL_PROTECTION_AREA', 'SSSI', 'COUNTRY_PARK',
    'MARINE_PARK', 'DECLARED_MONUMENT', 'FISH_CULTURE_ZONE', 'BATHING_BEACH',
    'EDUCATIONAL_INSTITUTION', 'HEALTH_CARE_INSTITUTION', 'PLACE_OF_WORSHIP',
    'RESIDENTIAL_AREA'
);

-- Statutory planning zones. Abbreviations kept from the source:
--   CDA  - Comprehensive Development Area
--   GIC  - Government, Institution or Community
--   LDC  - Land Development Corporation Development Scheme
--   SSSI - Site of Special Scientific Interest
--   URA  - Urban Renewal Authority Development Scheme
--   WKCD - West Kowloon Cultural District
CREATE TYPE land_category AS ENUM (
    'Agriculture', 'Coastal Protection', 'Commercial', 'Mixed Use', 'CDA',
    'Conservation', 'Country Park', 'Drainage', 'GIC', 'Green Belt',
    'Industrial', 'Inlet', 'LDC Dev Scheme', 'Major Road', 'Marine Basin',
    'Nullah', 'Open Space', 'Open Storage', 'Other Specified Uses',
    'Pedestrian Precinct', 'Pedestrian Street', 'Railway', 'Recreation',
    'Recreation Priority', 'Residential', 'River', 'Sea', 'SSSI',
    'Typhoon Shelter', 'Undetermined', 'URA Dev Scheme', 'Vessel Anchorage',
    'Village', 'WKCD'
);

-- Abbreviations kept from the source:
--   BHCB - Building Height Control in metres (above principal datum)
--   BHCS - Building Height Control in storeys
--   BHCN - Building Height Control specified in notes
--   NBA  - Non-Building Area (no structures permitted)
CREATE TYPE building_height_control_type AS ENUM ('BHCB', 'BHCS', 'BHCN', 'NBA');

-- ---------------------------------------------------------------------------
-- Air quality
-- ---------------------------------------------------------------------------

-- Air quality monitoring stations: name in English, address, coordinates,
-- altitude, station type and area type.
--
-- `station_code` is an addition to the upstream DDL: without a stable natural key
-- the monthly ingest cannot upsert a station and would create duplicates on every
-- run. It is also what station_map.py keys on.
CREATE TABLE air_quality_station (
    id           serial PRIMARY KEY,
    station_code varchar(20) UNIQUE,
    name_en      varchar(200),
    address_en   varchar(500),
    coordinate   geography(Point, 4326),
    altitude     double precision,
    station_type air_quality_station_type_enum,
    created_at   timestamp DEFAULT now(),
    area_type    air_quality_station_area_type_enum
);

-- Hourly measurements per station. All concentrations are in micrograms per
-- cubic metre (ug/m3). AQHI plus the "AR" (added health risk) contributions.
CREATE TABLE hourly_station_air_quality (
    id          serial PRIMARY KEY,
    station_id  integer REFERENCES air_quality_station,
    aqhi        integer,
    pm2_5       double precision,
    pm10        double precision,
    o3          double precision,
    so2         double precision,
    no2         double precision,
    co          double precision,
    no2_ar      double precision,
    o3_ar       double precision,
    pm_ar       double precision,
    so2_ar      double precision,
    pm_ar_source pm_ar_source_enum,
    datetime    timestamp,
    aqhi_level  air_quality_health_index_level_enum,
    UNIQUE (station_id, datetime)
);

-- ---------------------------------------------------------------------------
-- Roads and traffic
-- ---------------------------------------------------------------------------

CREATE TABLE street (
    id          serial PRIMARY KEY,
    street_type street_type,
    name_en     varchar(100),
    street_code varchar(20) UNIQUE,
    created_at  timestamp DEFAULT now()
);

CREATE TABLE road_segment (
    id                serial PRIMARY KEY,
    street_id         integer REFERENCES street,
    road_segment_code varchar(20) UNIQUE,
    length            smallint,
    road_level        smallint,
    direction         road_segment_direction,
    geometry          geometry(MultiLineString, 4326),
    free_flow_speed   real,
    created_at        timestamp DEFAULT now()
);

-- Hourly traffic per road segment. congestion_status is 0 smooth, 1 slight,
-- 2 moderate, 3 heavy. congestion_index is <=1.0 smooth, 1.0-1.5 slight,
-- 1.5-2.0 moderate, >2.0 heavy.
CREATE TABLE hourly_road_segment_traffic (
    id               serial PRIMARY KEY,
    road_segment_id  integer REFERENCES road_segment,
    speed            real,
    congestion_status smallint,
    congestion_index real,
    datetime         timestamp,
    UNIQUE (road_segment_id, datetime)
);

-- ---------------------------------------------------------------------------
-- Weather
-- ---------------------------------------------------------------------------

CREATE TABLE weather_station (
    id           serial PRIMARY KEY,
    name_en      varchar(200),
    station_code varchar(50) UNIQUE,
    address_en   varchar(500),
    coordinate   geography(Point, 4326),
    altitude     double precision,
    station_type weather_station_type_enum,
    created_at   timestamp DEFAULT now()
);

-- Hourly weather measurements. wind_degree is the numeric bearing, wind
-- direction is the binned compass value.
CREATE TABLE hourly_station_weather (
    id             serial PRIMARY KEY,
    station_id     integer REFERENCES weather_station,
    temperature    double precision,
    visibility     double precision,
    pressure       double precision,
    humidity       double precision,
    wind_degree    double precision,
    wind_speed     double precision,
    wind_gust      double precision,
    wind_direction wind_direction_enum,
    datetime       timestamp,
    UNIQUE (station_id, datetime)
);

-- ---------------------------------------------------------------------------
-- OpenStreetMap
-- ---------------------------------------------------------------------------

-- Very large table, query it by geometry or by main_type, never scan blindly.
CREATE TABLE osm_feature (
    id            serial PRIMARY KEY,
    name          varchar(255),
    sub_type      varchar(100),
    geometry      geometry(Geometry, 4326),
    geometry_type geometry_type_enum,
    main_type     osm_feature_main_type_enum
);

-- Added: makes the radius queries of Day 8 (schools/hospitals within 1.5-2 km)
-- usable instead of a sequential scan over every OSM way in Hong Kong.
CREATE INDEX osm_feature_geometry_gix ON osm_feature USING gist (geometry);

-- ---------------------------------------------------------------------------
-- EIA register
-- ---------------------------------------------------------------------------

CREATE TABLE eia_project (
    id                 serial PRIMARY KEY,
    name               varchar(300),
    eid                varchar(10) UNIQUE,
    application_number varchar(100),
    register_number    varchar(100),
    applicant          varchar(200),
    project_website    varchar(300),
    application_status eia_application_status_enum,
    coordinate         geography(Point, 4326),
    year               smallint
);

CREATE TABLE eia_habitat (
    id             serial PRIMARY KEY,
    geometry       geometry(Geometry, 4326),
    district       district_enum,
    eia_project_id integer REFERENCES eia_project,
    area           double precision,
    habitat_type   eia_habitat_type_enum
);

CREATE TABLE traffic_camera (
    code       varchar(20) UNIQUE,
    description varchar(500),
    district   district_enum,
    coordinate geography(Point, 4326),
    created_at timestamp DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Geographic and planning data
-- ---------------------------------------------------------------------------

-- Sensitive areas: protected or regulated zones requiring special
-- consideration in planning. `area` is in square metres.
CREATE TABLE sensitive_area (
    id            serial PRIMARY KEY,
    category      sensitive_area_category_enum NOT NULL,
    name          varchar(255),
    area          integer,
    geometry      geometry(Geometry, 4326) NOT NULL,
    geometry_type geometry_type_enum NOT NULL
);

-- Land use zones: statutory planning zones defining permitted land uses.
-- `area` is in square metres.
CREATE TABLE land_use_zone (
    id             serial PRIMARY KEY,
    zone_label     varchar(50) NOT NULL,
    land_category  land_category NOT NULL,
    specified_use  varchar(255),
    description    varchar(255),
    geometry       geometry(MultiPolygon, 4326) NOT NULL,
    area           integer
);

-- Building height controls: maximum building height restrictions per zone.
-- `area` is in square metres.
CREATE TABLE building_height_control (
    id           serial PRIMARY KEY,
    control_type building_height_control_type NOT NULL,
    height_value smallint,
    geometry     geometry(MultiPolygon, 4326) NOT NULL,
    area         integer
);
