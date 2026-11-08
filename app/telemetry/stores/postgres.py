import json
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from telemetry.config import SQL_DIR
from telemetry.generator import Assignment, Point, Track


class PostgresTelemetry:
    """Relational telemetry: a track is spread over tracks, track_points, sensor_readings, track_events."""

    name = "PostgreSQL"

    def __init__(self, conninfo: str) -> None:
        self.conn = psycopg.connect(conninfo, autocommit=True, row_factory=dict_row,
                                    options="-c timezone=UTC -c search_path=telemetry,fleet,public"
                                    # generic plans of auto-prepared statements ignore the search point
                                    " -c plan_cache_mode=force_custom_plan")
        self._sensor_ids: dict[str, int] = {}
        self._event_ids: dict[str, int] = {}

    def close(self) -> None:
        self.conn.close()

    def version(self) -> str:
        return self.conn.execute("SHOW server_version").fetchone()["server_version"]

    def _load_lookups(self) -> None:
        self._sensor_ids = {r["code"]: r["id"] for r in self.conn.execute("SELECT id, code FROM sensor_types")}
        self._event_ids = {r["code"]: r["id"] for r in self.conn.execute("SELECT id, code FROM event_types")}

    # ---------- setup ----------

    def setup(self) -> None:
        self.conn.execute((SQL_DIR / "telemetry.sql").read_text(encoding="utf-8"))
        self._load_lookups()

    def create_indexes(self) -> None:
        self.conn.execute((SQL_DIR / "telemetry_indexes.sql").read_text(encoding="utf-8"))

    def assignments(self) -> list[Assignment]:
        # every vehicle with drivers allowed to drive it (lab1 function fn_driver_can_drive)
        rows = self.conn.execute("""
            SELECT v.id AS vehicle_id, v.plate_number, v.vehicle_type, v.fuel_type,
                   d.id AS driver_id, d.full_name
            FROM fleet.v_vehicles v
            JOIN fleet.v_drivers d ON fleet.fn_driver_can_drive(d.id, v.id)
            ORDER BY v.id, d.id""").fetchall()
        return [Assignment(r["vehicle_id"], r["plate_number"], r["vehicle_type"], r["fuel_type"],
                           r["driver_id"], r["full_name"]) for r in rows]

    def stats(self) -> dict[str, int]:
        q = """SELECT (SELECT count(*) FROM tracks) AS tracks, (SELECT count(*) FROM track_points) AS points,
                      (SELECT count(*) FROM sensor_readings) AS readings, (SELECT count(*) FROM track_events) AS events,
                      (SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'telemetry' AND c.relkind = 'r' AND c.relname <> 'track_documents') AS bytes,
                      pg_total_relation_size('track_documents') AS jsonb_bytes"""
        return {k: int(v) for k, v in self.conn.execute(q).fetchone().items()}

    # writes

    def load(self, tracks: list[Track]) -> None:
        if not self._sensor_ids:
            self._load_lookups()
        with self.conn.transaction(), self.conn.cursor() as cur:
            with cur.copy("COPY tracks (id, vehicle_id, driver_id, started_at, ended_at, distance_km, max_speed, "
                          "avg_speed, points_count) FROM STDIN") as cp:
                for t in tracks:
                    cp.write_row((t.id, t.vehicle["id"], t.driver["id"], t.started_at, t.ended_at, t.distance_km,
                                  t.max_speed, t.avg_speed, len(t.points)))
            with cur.copy("COPY track_points (track_id, seq, recorded_at, lat, lon, speed, heading) FROM STDIN") as cp:
                for t in tracks:
                    for p in t.points:
                        cp.write_row((t.id, p.seq, p.t, p.lat, p.lon, p.speed, p.heading))
            with cur.copy("COPY sensor_readings (track_id, seq, sensor_type_id, value) FROM STDIN") as cp:
                for t in tracks:
                    for p in t.points:
                        for code, value in p.sensors.items():
                            cp.write_row((t.id, p.seq, self._sensor_ids[code], value))
            with cur.copy("COPY track_events (track_id, event_type_id, recorded_at, lat, lon, severity, value) "
                          "FROM STDIN") as cp:
                for t in tracks:
                    for e in t.events:
                        cp.write_row((t.id, self._event_ids[e.type], e.t, e.lat, e.lon, e.severity, e.value))

    def load_documents(self, docs: list[dict]) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            with cur.copy("COPY track_documents (id, vehicle_id, started_at, doc) FROM STDIN") as cp:
                for d in docs:
                    cp.write_row((d["_id"], d["vehicle"]["id"], d["started_at"], json.dumps(d, default=str)))

    def start_track(self, t: Track) -> None:
        self.conn.execute("INSERT INTO tracks (id, vehicle_id, driver_id, started_at) VALUES (%s, %s, %s, %s)",
                          (t.id, t.vehicle["id"], t.driver["id"], t.started_at))

    def append_point(self, track_id: int, p: Point) -> None:
        # live tracker message: point + its sensors + summary, one transaction
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("INSERT INTO track_points (track_id, seq, recorded_at, lat, lon, speed, heading) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (track_id, p.seq, p.t, p.lat, p.lon, p.speed, p.heading))
            cur.executemany("INSERT INTO sensor_readings (track_id, seq, sensor_type_id, value) VALUES (%s, %s, %s, %s)",
                            [(track_id, p.seq, self._sensor_ids[c], v) for c, v in p.sensors.items()])
            cur.execute("UPDATE tracks SET points_count = points_count + 1, max_speed = greatest(max_speed, %s), "
                        "ended_at = %s WHERE id = %s", (p.speed, p.t, track_id))

    def start_document(self, doc: dict) -> None:
        self.conn.execute("INSERT INTO track_documents (id, vehicle_id, started_at, doc) VALUES (%s, %s, %s, %s)",
                          (doc["_id"], doc["vehicle"]["id"], doc["started_at"], json.dumps(doc, default=str)))

    def append_point_document(self, track_id: int, point: dict) -> None:
        # jsonb is immutable: appending means rewriting the whole value
        self.conn.execute("""
            UPDATE track_documents
            SET doc = jsonb_set(jsonb_set(doc, '{points}', doc->'points' || %s::jsonb),
                                '{summary,points_count}', to_jsonb((doc->'summary'->>'points_count')::int + 1))
            WHERE id = %s""", (json.dumps([point], default=str), track_id))

    def delete_tracks(self, ids: list[int]) -> None:
        self.conn.execute("DELETE FROM tracks WHERE id = ANY(%s)", (ids,))
        self.conn.execute("DELETE FROM track_documents WHERE id = ANY(%s)", (ids,))

    # reads

    def get_track(self, track_id: int) -> dict | None:
        # typical app code: 3 queries and assembling the object
        head = self.conn.execute("""
            SELECT t.*, v.plate_number, vt.name AS vehicle_type, ft.name AS fuel_type,
                   concat_ws(' ', d.last_name, d.first_name, d.middle_name) AS driver_name
            FROM tracks t
            JOIN fleet.vehicles v ON v.id = t.vehicle_id
            JOIN fleet.vehicle_models m ON m.id = v.model_id
            JOIN fleet.vehicle_types vt ON vt.id = m.vehicle_type_id
            JOIN fleet.fuel_types ft ON ft.id = m.fuel_type_id
            JOIN fleet.drivers d ON d.id = t.driver_id
            WHERE t.id = %s""", (track_id,)).fetchone()
        if head is None:
            return None
        points: list[dict] = []
        for r in self.conn.execute("""
                SELECT p.seq, p.recorded_at, p.lat, p.lon, p.speed, p.heading, s.code, r.value
                FROM track_points p
                LEFT JOIN sensor_readings r ON r.track_id = p.track_id AND r.seq = p.seq
                LEFT JOIN sensor_types s ON s.id = r.sensor_type_id
                WHERE p.track_id = %s
                ORDER BY p.seq""", (track_id,)):
            if not points or points[-1]["seq"] != r["seq"]:
                points.append({"seq": r["seq"], "t": r["recorded_at"], "lat": r["lat"], "lon": r["lon"],
                               "speed": r["speed"], "heading": r["heading"], "sensors": {}})
            if r["code"] is not None:
                points[-1]["sensors"][r["code"]] = r["value"]
        events = self.conn.execute("""
            SELECT et.code AS type, e.recorded_at AS t, e.lat, e.lon, e.severity, e.value
            FROM track_events e JOIN event_types et ON et.id = e.event_type_id
            WHERE e.track_id = %s ORDER BY e.recorded_at, e.id""", (track_id,)).fetchall()
        return {"id": head["id"],
                "vehicle": {"id": head["vehicle_id"], "plate": head["plate_number"], "type": head["vehicle_type"],
                            "fuel": head["fuel_type"]},
                "driver": {"id": head["driver_id"], "name": head["driver_name"]},
                "started_at": head["started_at"], "ended_at": head["ended_at"],
                "summary": {"distance_km": head["distance_km"], "max_speed": head["max_speed"],
                            "avg_speed": head["avg_speed"], "points_count": head["points_count"]},
                "points": points, "events": [dict(e) for e in events]}

    def get_track_single_query(self, track_id: int) -> dict | None:
        # the same object built by PostgreSQL itself with json functions
        row = self.conn.execute("""
            SELECT json_build_object(
                'id', t.id,
                'vehicle', json_build_object('id', v.id, 'plate', v.plate_number, 'type', vt.name, 'fuel', ft.name),
                'driver', json_build_object('id', d.id, 'name', concat_ws(' ', d.last_name, d.first_name, d.middle_name)),
                'started_at', t.started_at, 'ended_at', t.ended_at,
                'summary', json_build_object('distance_km', t.distance_km, 'max_speed', t.max_speed,
                                             'avg_speed', t.avg_speed, 'points_count', t.points_count),
                'points', (SELECT json_agg(json_build_object(
                                  'seq', p.seq, 't', p.recorded_at, 'lat', p.lat, 'lon', p.lon, 'speed', p.speed,
                                  'heading', p.heading,
                                  'sensors', (SELECT coalesce(json_object_agg(s.code, r.value), '{}')
                                              FROM sensor_readings r JOIN sensor_types s ON s.id = r.sensor_type_id
                                              WHERE r.track_id = p.track_id AND r.seq = p.seq))
                                  ORDER BY p.seq)
                           FROM track_points p WHERE p.track_id = t.id),
                'events', (SELECT coalesce(json_agg(json_build_object(
                                  'type', et.code, 't', e.recorded_at, 'lat', e.lat, 'lon', e.lon,
                                  'severity', e.severity, 'value', e.value) ORDER BY e.recorded_at, e.id), '[]')
                           FROM track_events e JOIN event_types et ON et.id = e.event_type_id
                           WHERE e.track_id = t.id)) AS doc
            FROM tracks t
            JOIN fleet.vehicles v ON v.id = t.vehicle_id
            JOIN fleet.vehicle_models m ON m.id = v.model_id
            JOIN fleet.vehicle_types vt ON vt.id = m.vehicle_type_id
            JOIN fleet.fuel_types ft ON ft.id = m.fuel_type_id
            JOIN fleet.drivers d ON d.id = t.driver_id
            WHERE t.id = %s""", (track_id,)).fetchone()
        return row["doc"] if row else None

    def get_document(self, track_id: int) -> dict | None:
        row = self.conn.execute("SELECT doc FROM track_documents WHERE id = %s", (track_id,)).fetchone()
        return row["doc"] if row else None

    def vehicle_tracks(self, vehicle_id: int, since: datetime) -> list[tuple]:
        rows = self.conn.execute("""
            SELECT id, started_at, distance_km FROM tracks
            WHERE vehicle_id = %s AND started_at >= %s ORDER BY started_at DESC""", (vehicle_id, since))
        return [(r["id"], r["started_at"], round(r["distance_km"], 2)) for r in rows]

    def monthly_stats(self) -> list[tuple]:
        rows = self.conn.execute("""
            SELECT t.vehicle_id, date_trunc('month', t.started_at)::date AS month, count(*) AS tracks,
                   sum(t.distance_km) AS km, max(t.max_speed) AS max_speed, coalesce(sum(e.cnt), 0) AS events
            FROM tracks t
            LEFT JOIN (SELECT track_id, count(*) AS cnt FROM track_events GROUP BY track_id) e ON e.track_id = t.id
            GROUP BY 1, 2 ORDER BY 1, 2""")
        return [(r["vehicle_id"], r["month"], r["tracks"], round(r["km"], 0), r["max_speed"], int(r["events"]))
                for r in rows]

    def speed_by_hour(self) -> list[tuple]:
        rows = self.conn.execute("""
            SELECT extract(hour FROM recorded_at)::int AS hour, count(*) AS n, avg(speed) AS avg_speed
            FROM track_points GROUP BY 1 ORDER BY 1""")
        return [(r["hour"], r["n"], round(r["avg_speed"], 2)) for r in rows]

    def tracks_near(self, lat: float, lon: float, radius_m: float, since: datetime, until: datetime) -> list[int]:
        rows = self.conn.execute("""
            SELECT DISTINCT p.track_id
            FROM track_points p JOIN tracks t ON t.id = p.track_id
            WHERE earth_box(ll_to_earth(%(lat)s, %(lon)s), %(r)s) @> ll_to_earth(p.lat, p.lon)
              AND earth_distance(ll_to_earth(%(lat)s, %(lon)s), ll_to_earth(p.lat, p.lon)) <= %(r)s
              AND t.started_at BETWEEN %(since)s AND %(until)s
            ORDER BY 1""", {"lat": lat, "lon": lon, "r": radius_m, "since": since, "until": until})
        return [r["track_id"] for r in rows]

    def cargo_violations(self, threshold: float) -> list[tuple]:
        # seq grows with time, so the first violation is min(seq), then one primary key lookup per trip
        rows = self.conn.execute("""
            SELECT f.track_id, p.recorded_at AS first_at
            FROM (SELECT track_id, min(seq) AS seq
                  FROM sensor_readings
                  WHERE sensor_type_id = (SELECT id FROM sensor_types WHERE code = 'cargo_temp') AND value > %s
                  GROUP BY track_id) f
            JOIN track_points p ON p.track_id = f.track_id AND p.seq = f.seq
            ORDER BY 1""", (threshold,))
        return [(r["track_id"], r["first_at"]) for r in rows]

    def random_locations(self, count: int, seed: int) -> list[tuple[float, float]]:
        rows = self.conn.execute("""
            SELECT lat, lon FROM track_points
            WHERE (track_id * 7919 + seq * 104729 + %s) %% 15013 = 0 ORDER BY track_id, seq LIMIT %s""",
                                 (seed, count))
        return [(r["lat"], r["lon"]) for r in rows]
