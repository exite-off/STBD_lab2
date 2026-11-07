from datetime import datetime

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection

from telemetry.generator import Point, Track

EARTH_RADIUS_M = 6378168  # the same radius as PostgreSQL earthdistance


def point_doc(p: Point) -> dict:
    return {"seq": p.seq, "t": p.t, "loc": {"type": "Point", "coordinates": [p.lon, p.lat]},
            "speed": p.speed, "heading": p.heading, "sensors": p.sensors}


def track_doc(t: Track) -> dict:
    # one document = whole trip, vehicle and driver are embedded (denormalized)
    return {
        "_id": t.id,
        "vehicle": t.vehicle,
        "driver": t.driver,
        "started_at": t.started_at,
        "ended_at": t.ended_at,
        "summary": {"distance_km": t.distance_km, "max_speed": t.max_speed, "avg_speed": t.avg_speed,
                    "points_count": len(t.points)},
        "points": [point_doc(p) for p in t.points],
        "events": [{"type": e.type, "t": e.t, "loc": {"type": "Point", "coordinates": [e.lon, e.lat]},
                    "severity": e.severity, "value": e.value} for e in t.events],
    }


class MongoTelemetry:
    name = "MongoDB"

    def __init__(self, uri: str, database: str) -> None:
        self.client = MongoClient(uri, tz_aware=True)
        self.db = self.client[database]
        self.tracks: Collection = self.db["tracks"]

    def close(self) -> None:
        self.client.close()

    def version(self) -> str:
        return self.client.server_info()["version"]

    def setup(self) -> None:
        self.tracks.drop()

    def create_indexes(self) -> None:
        self.tracks.create_index([("vehicle.id", ASCENDING), ("started_at", DESCENDING)])
        self.tracks.create_index([("driver.id", ASCENDING), ("started_at", DESCENDING)])
        self.tracks.create_index([("started_at", ASCENDING)])
        self.tracks.create_index([("points.loc", "2dsphere")])
        self.tracks.create_index([("points.sensors.cargo_temp", ASCENDING)], sparse=True)

    def stats(self) -> dict[str, int]:
        self.client.admin.command("fsync")  # flush, otherwise storageSize lags behind
        s = self.db.command("collStats", "tracks")
        return {"tracks": s["count"], "bytes": s["storageSize"] + s["totalIndexSize"], "data_bytes": s["size"]}

    # writes

    def load(self, docs: list[dict]) -> None:
        self.tracks.insert_many(docs, ordered=False)

    def start_track(self, doc: dict) -> None:
        self.tracks.insert_one(doc)

    def append_point(self, track_id: int, p: Point) -> None:
        # one atomic update: push the point and keep the summary in sync
        self.tracks.update_one({"_id": track_id}, {
            "$push": {"points": point_doc(p)},
            "$inc": {"summary.points_count": 1},
            "$max": {"summary.max_speed": p.speed},
            "$set": {"ended_at": p.t},
        })

    def delete_tracks(self, ids: list[int]) -> None:
        self.tracks.delete_many({"_id": {"$in": ids}})

    # reads

    def get_track(self, track_id: int) -> dict | None:
        return self.tracks.find_one({"_id": track_id})

    def vehicle_tracks(self, vehicle_id: int, since: datetime) -> list[tuple]:
        cur = self.tracks.find({"vehicle.id": vehicle_id, "started_at": {"$gte": since}},
                               {"started_at": 1, "summary.distance_km": 1}).sort("started_at", DESCENDING)
        return [(d["_id"], d["started_at"], round(d["summary"]["distance_km"], 2)) for d in cur]

    def monthly_stats(self) -> list[tuple]:
        rows = self.tracks.aggregate([
            {"$group": {
                "_id": {"v": "$vehicle.id", "m": {"$dateTrunc": {"date": "$started_at", "unit": "month"}}},
                "tracks": {"$sum": 1},
                "km": {"$sum": "$summary.distance_km"},
                "max_speed": {"$max": "$summary.max_speed"},
                "events": {"$sum": {"$size": "$events"}},
            }},
            {"$sort": {"_id.v": 1, "_id.m": 1}},
        ])
        return [(r["_id"]["v"], r["_id"]["m"].date(), r["tracks"], round(r["km"], 0), r["max_speed"], r["events"])
                for r in rows]

    def speed_by_hour(self) -> list[tuple]:
        rows = self.tracks.aggregate([
            {"$unwind": "$points"},
            {"$group": {"_id": {"$hour": "$points.t"}, "n": {"$sum": 1}, "avg": {"$avg": "$points.speed"}}},
            {"$sort": {"_id": 1}},
        ], allowDiskUse=True)
        return [(r["_id"], r["n"], round(r["avg"], 2)) for r in rows]

    def tracks_near(self, lat: float, lon: float, radius_m: float, since: datetime, until: datetime) -> list[int]:
        cur = self.tracks.find({
            "points.loc": {"$geoWithin": {"$centerSphere": [[lon, lat], radius_m / EARTH_RADIUS_M]}},
            "started_at": {"$gte": since, "$lte": until},
        }, {"_id": 1}).sort("_id", ASCENDING)
        return [d["_id"] for d in cur]

    def cargo_violations(self, threshold: float) -> list[tuple]:
        rows = self.tracks.aggregate([
            {"$match": {"points.sensors.cargo_temp": {"$gt": threshold}}},
            {"$project": {"first_at": {"$min": {"$map": {
                "input": {"$filter": {"input": "$points", "cond": {"$gt": ["$$this.sensors.cargo_temp", threshold]}}},
                "in": "$$this.t"}}}}},
            {"$sort": {"_id": 1}},
        ])
        return [(r["_id"], r["first_at"]) for r in rows]
