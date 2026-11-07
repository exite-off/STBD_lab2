import time
from itertools import batched

from telemetry.generator import generate
from telemetry.stores.mongo import MongoTelemetry, track_doc
from telemetry.stores.postgres import PostgresTelemetry


def load_all(pg: PostgresTelemetry, mongo: MongoTelemetry, tracks: int, seed: int = 42,
             batch_size: int = 100) -> dict:
    """Generates the same tracks once and loads them into every store, timing each store separately."""
    pg.setup()
    mongo.setup()
    assignments = pg.assignments()
    if not assignments:
        raise RuntimeError("No vehicles with allowed drivers found, is the lab1 database loaded?")

    spent = {"PostgreSQL": 0.0, "PostgreSQL JSONB": 0.0, "MongoDB": 0.0}
    loaded = 0
    for batch in batched(generate(assignments, tracks, seed), batch_size):
        t0 = time.perf_counter()
        pg.load(list(batch))
        t1 = time.perf_counter()
        # building documents is part of the document stores cost
        docs = [track_doc(t) for t in batch]
        t2 = time.perf_counter()
        mongo.load(docs)
        t3 = time.perf_counter()
        pg.load_documents(docs)
        t4 = time.perf_counter()
        spent["PostgreSQL"] += t1 - t0
        spent["MongoDB"] += (t2 - t1) + (t3 - t2)
        spent["PostgreSQL JSONB"] += (t2 - t1) + (t4 - t3)
        loaded += len(batch)
        print(f"\r  loaded {loaded}/{tracks} tracks", end="", flush=True)
    print()

    t0 = time.perf_counter()
    pg.create_indexes()
    t1 = time.perf_counter()
    mongo.create_indexes()
    t2 = time.perf_counter()

    return {
        "tracks": tracks,
        "seed": seed,
        "batch_size": batch_size,
        "load_seconds": {k: round(v, 2) for k, v in spent.items()},
        "index_seconds": {"PostgreSQL": round(t1 - t0, 2), "MongoDB": round(t2 - t1, 2)},
        "postgres": pg.stats(),
        "mongo": mongo.stats(),
    }
