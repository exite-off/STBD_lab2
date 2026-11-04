-- Indexes are created after bulk load, the same set of access paths as in MongoDB

CREATE INDEX ix_tracks_vehicle_started ON telemetry.tracks (vehicle_id, started_at DESC);
CREATE INDEX ix_tracks_driver_started ON telemetry.tracks (driver_id, started_at DESC);
CREATE INDEX ix_tracks_started ON telemetry.tracks (started_at);

-- spherical geo search: GiST over earthdistance cube
CREATE INDEX ix_points_earth ON telemetry.track_points USING gist (ll_to_earth(lat, lon));
CREATE INDEX ix_points_recorded_brin ON telemetry.track_points USING brin (recorded_at);

CREATE INDEX ix_readings_type_value ON telemetry.sensor_readings (sensor_type_id, value);

CREATE INDEX ix_events_track ON telemetry.track_events (track_id);
CREATE INDEX ix_events_type_recorded ON telemetry.track_events (event_type_id, recorded_at);

CREATE INDEX ix_track_documents_vehicle ON telemetry.track_documents (vehicle_id, started_at DESC);

ANALYZE telemetry.tracks;
ANALYZE telemetry.track_points;
ANALYZE telemetry.sensor_readings;
ANALYZE telemetry.track_events;
ANALYZE telemetry.track_documents;
