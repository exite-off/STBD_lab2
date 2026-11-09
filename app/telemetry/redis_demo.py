import json
import time
from datetime import UTC, datetime, timedelta

import redis

from telemetry import config
from telemetry.stores.mongo import MongoTelemetry
from telemetry.stores.postgres import PostgresTelemetry

KYIV_CENTER = (30.5234, 50.4501)  # lon, lat


def title(text: str) -> None:
    print(f"\n=== {text} ===")


def cmd(text: str) -> None:
    print(f"  > {text}")


def latest_positions(mongo: MongoTelemetry) -> list[dict]:
    # last point of the latest trip of every vehicle
    return list(mongo.tracks.aggregate([
        {"$sort": {"started_at": -1}},
        {"$group": {"_id": "$vehicle.id", "vehicle": {"$first": "$vehicle"}, "driver": {"$first": "$driver"},
                    "last": {"$first": {"$last": "$points"}}}},
        {"$sort": {"_id": 1}},
    ]))


def driver_km(mongo: MongoTelemetry, since: datetime) -> list[dict]:
    return list(mongo.tracks.aggregate([
        {"$match": {"started_at": {"$gte": since}}},
        {"$group": {"_id": "$driver.name", "km": {"$sum": "$summary.distance_km"}}},
    ]))


def run_demo() -> None:
    r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    mongo = MongoTelemetry(config.MONGO_URI, config.MONGO_DB)
    pg = PostgresTelemetry(config.PG_CONNINFO)
    try:
        r.flushdb()
        positions = latest_positions(mongo)
        if not positions:
            raise RuntimeError("MongoDB has no tracks, run `python -m telemetry load` first")

        title("1. Hash: current state of every vehicle")
        for p in positions:
            lon, lat = p["last"]["loc"]["coordinates"]
            r.hset(f"vehicle:{p['_id']}:state", mapping={
                "plate": p["vehicle"]["plate"], "driver": p["driver"]["name"], "lat": lat, "lon": lon,
                "speed": p["last"]["speed"], "updated_at": p["last"]["t"].isoformat()})
        first = positions[0]["_id"]
        cmd(f"HSET vehicle:{first}:state plate ... lat ... lon ... speed ...   (for {len(positions)} vehicles)")
        cmd(f"HGETALL vehicle:{first}:state")
        print("   ", r.hgetall(f"vehicle:{first}:state"))
        cmd(f"HINCRBYFLOAT vehicle:{first}:state speed 5")
        print("   ", r.hincrbyfloat(f"vehicle:{first}:state", "speed", 5))

        title("2. String with TTL: online status (tracker heartbeat)")
        online = [p["_id"] for p in positions[: len(positions) // 2 + 1]]
        for vid in online:
            r.set(f"vehicle:{vid}:online", 1, ex=60)
        cmd("SET vehicle:<id>:online 1 EX 60   (heartbeat every 30 s, key expires if the tracker is silent)")
        for p in positions:
            vid = p["_id"]
            ttl = r.ttl(f"vehicle:{vid}:online")
            state = f"online, expires in {ttl} s" if ttl > 0 else "offline"
            print(f"    {p['vehicle']['plate']:<9} {state}")

        title("3. Geo: nearest vehicles to a dispatcher call")
        for p in positions:
            lon, lat = p["last"]["loc"]["coordinates"]
            r.geoadd("fleet:positions", (lon, lat, p["vehicle"]["plate"]))
        cmd("GEOADD fleet:positions <lon> <lat> <plate>   (for every vehicle)")
        cmd(f"GEOSEARCH fleet:positions FROMLONLAT {KYIV_CENTER[0]} {KYIV_CENTER[1]} BYRADIUS 150 km ASC COUNT 3 WITHDIST")
        for plate, dist in r.geosearch("fleet:positions", longitude=KYIV_CENTER[0], latitude=KYIV_CENTER[1],
                                       radius=150, unit="km", sort="ASC", count=3, withdist=True):
            print(f"    {plate:<9} {dist:7.2f} km")
        a, b = positions[0]["vehicle"]["plate"], positions[1]["vehicle"]["plate"]
        cmd(f"GEODIST fleet:positions {a} {b} km")
        print(f"    {r.geodist('fleet:positions', a, b, unit='km'):.2f} km")

        title("4. Sorted set: drivers leaderboard by km for 30 days")
        since = datetime.now(UTC) - timedelta(days=30)
        for row in driver_km(mongo, since):
            r.zadd("drivers:km:30d", {row["_id"]: round(row["km"], 1)})
        cmd("ZADD drivers:km:30d <km> <driver>   (km come from MongoDB aggregation)")
        cmd("ZREVRANGE drivers:km:30d 0 2 WITHSCORES")
        for name, km in r.zrevrange("drivers:km:30d", 0, 2, withscores=True):
            print(f"    {name:<32} {km:9.1f} km")
        last = r.zrange("drivers:km:30d", 0, 0)[0]
        cmd(f"ZINCRBY drivers:km:30d 250 \"{last}\"   (a new trip finished)")
        r.zincrby("drivers:km:30d", 250, last)
        cmd(f"ZREVRANK drivers:km:30d \"{last}\"")
        print(f"    place {r.zrevrank('drivers:km:30d', last) + 1} of {r.zcard('drivers:km:30d')}")

        title("5. Stream: queue of live tracker messages")
        for p in positions[:5]:
            lon, lat = p["last"]["loc"]["coordinates"]
            r.xadd("telemetry:live", {"vehicle_id": p["_id"], "lat": lat, "lon": lon, "speed": p["last"]["speed"]},
                   maxlen=10_000, approximate=True)
        cmd("XADD telemetry:live MAXLEN ~ 10000 * vehicle_id <id> lat <lat> lon <lon> speed <v>   (5 messages)")
        r.xgroup_create("telemetry:live", "mongo-writer", id="0")
        cmd("XGROUP CREATE telemetry:live mongo-writer 0")
        cmd("XREADGROUP GROUP mongo-writer worker-1 COUNT 3 STREAMS telemetry:live >")
        batch = r.xreadgroup("mongo-writer", "worker-1", {"telemetry:live": ">"}, count=3)
        ids = [msg_id for _, messages in batch for msg_id, _ in messages]
        for _, messages in batch:
            for msg_id, fields in messages:
                print(f"    {msg_id} {fields}")
        cmd(f"XACK telemetry:live mongo-writer {' '.join(ids)}   (written to MongoDB)")
        print(f"    acknowledged: {r.xack('telemetry:live', 'mongo-writer', *ids)}")
        cmd("XPENDING telemetry:live mongo-writer")
        print(f"    stream length {r.xlen('telemetry:live')}, pending {r.xpending('telemetry:live', 'mongo-writer')['pending']}")

        title("6. Counters: events per day")
        today = datetime.now(UTC).date().isoformat()
        for event_type, n in (("harsh_braking", 3), ("speeding", 5), ("harsh_braking", 1)):
            r.hincrby(f"events:{today}", event_type, n)
        cmd(f"HINCRBY events:{today} harsh_braking 3  /  speeding 5  /  harsh_braking 1")
        cmd(f"HGETALL events:{today}")
        print("   ", r.hgetall(f"events:{today}"))
        r.expire(f"events:{today}", 7 * 24 * 3600)

        title("7. Cache-aside: vehicle statistics from PostgreSQL")
        key = "cache:vehicle_stats"
        for attempt in ("miss", "hit"):
            t0 = time.perf_counter()
            cached = r.get(key)
            if cached is None:
                rows = pg.conn.execute("SELECT plate_number, trips_completed, trips_km FROM fleet.v_vehicle_stats "
                                       "ORDER BY plate_number").fetchall()
                r.set(key, json.dumps(rows, default=str), ex=30)
                source = "PostgreSQL view v_vehicle_stats, saved with SET ... EX 30"
            else:
                rows = json.loads(cached)
                source = "Redis GET"
            ms = (time.perf_counter() - t0) * 1000
            print(f"    {attempt}: {len(rows)} rows from {source}, {ms:.2f} ms")
        cmd(f"TTL {key}")
        print(f"    {r.ttl(key)} s left")

        title("Keys created")
        cmd("SCAN 0 COUNT 100")
        print("   ", sorted(r.scan_iter(count=100)))
    finally:
        r.close()
        mongo.close()
        pg.close()
