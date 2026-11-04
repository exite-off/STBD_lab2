-- Vehicle telemetry, relational version of the MongoDB "tracks" collection
-- One track document maps to: tracks, track_points, sensor_readings, track_events (+ 2 lookup tables)

CREATE EXTENSION IF NOT EXISTS cube;
CREATE EXTENSION IF NOT EXISTS earthdistance;

DROP SCHEMA IF EXISTS telemetry CASCADE;
CREATE SCHEMA telemetry;
SET search_path = telemetry, fleet, public;

CREATE TABLE telemetry.sensor_types (
    id smallint PRIMARY KEY,
    code varchar(30) NOT NULL UNIQUE,
    unit varchar(10) NOT NULL,
    description varchar(100) NOT NULL
);

CREATE TABLE telemetry.event_types (
    id smallint PRIMARY KEY,
    code varchar(30) NOT NULL UNIQUE,
    description varchar(100) NOT NULL
);

-- one row per recorded trip, summary is computed by the tracker
CREATE TABLE telemetry.tracks (
    id bigint PRIMARY KEY,
    vehicle_id integer NOT NULL REFERENCES fleet.vehicles (id),
    driver_id integer NOT NULL REFERENCES fleet.drivers (id),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    distance_km double precision NOT NULL DEFAULT 0,
    max_speed double precision NOT NULL DEFAULT 0,
    avg_speed double precision NOT NULL DEFAULT 0,
    points_count integer NOT NULL DEFAULT 0
);

CREATE TABLE telemetry.track_points (
    track_id bigint NOT NULL REFERENCES telemetry.tracks (id) ON DELETE CASCADE,
    seq integer NOT NULL,
    recorded_at timestamptz NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    speed double precision NOT NULL,
    heading smallint NOT NULL,
    PRIMARY KEY (track_id, seq)
);

-- sensors differ by vehicle type, so they are stored as attribute rows (EAV)
CREATE TABLE telemetry.sensor_readings (
    track_id bigint NOT NULL,
    seq integer NOT NULL,
    sensor_type_id smallint NOT NULL REFERENCES telemetry.sensor_types (id),
    value double precision NOT NULL,
    PRIMARY KEY (track_id, seq, sensor_type_id),
    FOREIGN KEY (track_id, seq) REFERENCES telemetry.track_points (track_id, seq) ON DELETE CASCADE
);

CREATE TABLE telemetry.track_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    track_id bigint NOT NULL REFERENCES telemetry.tracks (id) ON DELETE CASCADE,
    event_type_id smallint NOT NULL REFERENCES telemetry.event_types (id),
    recorded_at timestamptz NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    severity smallint NOT NULL,
    value double precision
);

-- document variant: the same track as one jsonb value, for comparison
CREATE TABLE telemetry.track_documents (
    id bigint PRIMARY KEY,
    vehicle_id integer NOT NULL,
    started_at timestamptz NOT NULL,
    doc jsonb NOT NULL
);

INSERT INTO telemetry.sensor_types (id, code, unit, description) VALUES
    (1, 'fuel_level', '%', 'Fuel tank level'),
    (2, 'engine_temp', 'C', 'Engine coolant temperature'),
    (3, 'battery_soc', '%', 'EV battery state of charge'),
    (4, 'battery_temp', 'C', 'EV battery temperature'),
    (5, 'cargo_temp', 'C', 'Refrigerated cargo temperature'),
    (6, 'axle_load', 't', 'Axle load'),
    (7, 'passengers', 'pcs', 'Passengers on board'),
    (8, 'door_open', 'bool', 'Door open sensor');

INSERT INTO telemetry.event_types (id, code, description) VALUES
    (1, 'harsh_braking', 'Speed dropped too fast'),
    (2, 'harsh_acceleration', 'Speed grew too fast'),
    (3, 'speeding', 'Speed above the limit for vehicle type'),
    (4, 'cargo_temp_alarm', 'Refrigerated cargo is too warm'),
    (5, 'door_open_moving', 'Door opened while moving');
