import json
import platform
import random
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from telemetry.config import RESULTS_DIR
from telemetry.generator import Track, generate
from telemetry.stores.mongo import MongoTelemetry, point_doc, track_doc
from telemetry.stores.postgres import PostgresTelemetry

type Call = Callable[[Any], Any]


@dataclass
class Scenario:
    key: str
    title: str
    why: str
    params: list[Any]
    engines: dict[str, Call]
    signature: Callable[[Any], Any] = lambda r: r
    warmup: int = 2
    per_op: bool = True
    verify: Callable[[], dict[str, Any]] | None = None


@dataclass
class Measured:
    engine: str
    times_ms: list[float] = field(default_factory=list)
    ok: bool = True

    @property
    def median(self) -> float:
        return statistics.median(self.times_ms)

    @property
    def p95(self) -> float:
        s = sorted(self.times_ms)
        return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.times_ms)


def run(s: Scenario) -> list[Measured]:
    print(f"\n[{s.key}] {s.title}")
    results: list[Measured] = []
    reference: list[Any] | None = None
    for engine, fn in s.engines.items():
        for p in s.params[:s.warmup]:
            fn(p)
        m = Measured(engine)
        signatures = []
        for p in s.params:
            t0 = time.perf_counter_ns()
            r = fn(p)
            m.times_ms.append((time.perf_counter_ns() - t0) / 1e6)
            signatures.append(s.signature(r))
        # every engine must return the same answer for the same input
        if reference is None:
            reference = signatures
        else:
            m.ok = signatures == reference
            if not m.ok:
                bad = next(i for i, (a, b) in enumerate(zip(signatures, reference)) if a != b)
                print(f"  ! {engine} differs at param #{bad}: {str(signatures[bad])[:120]} vs {str(reference[bad])[:120]}")
        results.append(m)
        if s.verify is None:
            print(f"  {engine:<26} median {m.median:9.2f} ms   p95 {m.p95:9.2f} ms   runs {len(m.times_ms):4}"
              f"   {'same result' if m.ok else 'DIFFERENT RESULT'}")
    if s.verify is not None:
        # writes have no answer to compare, so the final state is compared instead
        state = s.verify()
        same = len({json.dumps(v, sort_keys=True) for v in state.values()}) == 1
        for m in results:
            m.ok = same
            print(f"  {m.engine:<26} median {m.median:9.2f} ms   p95 {m.p95:9.2f} ms   runs {len(m.times_ms):4}"
                  f"   final state {'same' if same else 'DIFFERENT'}")
        print(f"  points per trip after appends: {state['MongoDB']}")
    return results


def track_signature(doc: dict | None) -> tuple | None:
    if doc is None:
        return None
    pts = doc["points"]
    return (doc["vehicle"]["plate"], doc["driver"]["name"], len(pts), len(doc["events"]),
            round(sum(p["speed"] for p in pts), 1), tuple(sorted(pts[0]["sensors"])) if pts else ())


def live_tracks(pg: PostgresTelemetry, count: int, seed: int, length: int) -> list[Track]:
    # only trips long enough for the scenario
    assignments = pg.assignments()
    picked = [t for t in generate(assignments, count * 6, seed=seed + 1, days=1, first_id=10_000_001)
              if len(t.points) >= length]
    return picked[:count]


def append_scenario(pg: PostgresTelemetry, mongo: MongoTelemetry, seed: int, tracks: int = 10,
                    initial: int = 400, live: int = 100) -> Scenario:
    """Tracker sends a point every 30 s: the trip already has `initial` points, `live` more arrive."""
    data = live_tracks(pg, tracks, seed, initial + live)
    ids = [t.id for t in data]
    pg.delete_tracks(ids)
    mongo.delete_tracks(ids)
    head = []
    for t in data:
        first = Track(t.id, t.vehicle, t.driver, t.started_at, t.points[initial - 1].t, t.distance_km,
                      t.max_speed, t.avg_speed, t.points[:initial], [])
        head.append(first)
    pg.load(head)
    docs = [track_doc(t) for t in head]
    mongo.load(docs)
    pg.load_documents(docs)

    # interleave tracks like real trackers do
    ops = [(t.id, t.points[initial + i]) for i in range(live) for t in data]
    return Scenario(
        key="W2", title=f"Live tracker: append one point to an active trip ({tracks} trips x {live} points)",
        why="Main write load of telemetry: small frequent writes into a growing trip.",
        params=ops, warmup=0,
        engines={
            "PostgreSQL": lambda op: pg.append_point(op[0], op[1]),
            "PostgreSQL JSONB": lambda op: pg.append_point_document(op[0], point_doc(op[1])),
            "MongoDB": lambda op: mongo.append_point(op[0], op[1]),
        },
        signature=lambda r: None,
        verify=lambda: {
            "PostgreSQL": [r["n"] for r in pg.conn.execute(
                "SELECT t.points_count AS n FROM tracks t WHERE t.id = ANY(%s) ORDER BY t.id", (ids,))],
            "PostgreSQL (rows)": [r["n"] for r in pg.conn.execute(
                "SELECT count(*) AS n FROM track_points WHERE track_id = ANY(%s) GROUP BY track_id ORDER BY track_id",
                (ids,))],
            "PostgreSQL JSONB": [r["n"] for r in pg.conn.execute(
                "SELECT jsonb_array_length(doc->'points') AS n FROM track_documents WHERE id = ANY(%s) ORDER BY id",
                (ids,))],
            "MongoDB": [d["summary"]["points_count"] for d in mongo.tracks.find(
                {"_id": {"$in": ids}}, {"summary.points_count": 1}).sort("_id", 1)],
        })


def scenarios(pg: PostgresTelemetry, mongo: MongoTelemetry, quick: bool, seed: int) -> list[Scenario]:
    rnd = random.Random(seed)
    max_id = pg.conn.execute("SELECT max(id) AS m FROM tracks WHERE id < 10000000").fetchone()["m"]
    track_ids = [rnd.randint(1, max_id) for _ in range(60 if quick else 300)]
    vehicle_ids = [r["vehicle_id"] for r in pg.conn.execute("SELECT DISTINCT vehicle_id FROM tracks ORDER BY 1")]
    now = datetime.now(UTC)
    since30 = now - timedelta(days=30)
    locations = pg.random_locations(20 if quick else 60, seed)
    window = (now - timedelta(days=90), now)
    heavy = 3 if quick else 5

    return [
        Scenario(
            "R1", "Read one whole trip (points + sensors + events) by id",
            "Show a trip on the map: the whole aggregate is needed at once.",
            track_ids,
            {
                "PostgreSQL (3 queries)": pg.get_track,
                "PostgreSQL (1 query, json_agg)": pg.get_track_single_query,
                "PostgreSQL JSONB": pg.get_document,
                "MongoDB": mongo.get_track,
            },
            signature=track_signature),
        Scenario(
            "R2", "List trips of a vehicle for the last 30 days (summary only)",
            "Typical list screen, only the summary fields, no points.",
            [v for v in vehicle_ids for _ in range(10 if quick else 30)],
            {
                "PostgreSQL": lambda v: pg.vehicle_tracks(v, since30),
                "MongoDB": lambda v: mongo.vehicle_tracks(v, since30),
            }),
        Scenario(
            "A1", "Monthly report per vehicle: trips, km, max speed, events",
            "Aggregation over trip summaries plus counting events of every trip.",
            list(range(heavy)),
            {"PostgreSQL": lambda _: pg.monthly_stats(), "MongoDB": lambda _: mongo.monthly_stats()},
            warmup=1, per_op=False),
        Scenario(
            "A2", "Average speed by hour of day over all GPS points",
            "Analytics over individual points: in MongoDB they are inside arrays and must be unwound.",
            list(range(heavy)),
            {"PostgreSQL": lambda _: pg.speed_by_hour(), "MongoDB": lambda _: mongo.speed_by_hour()},
            warmup=1, per_op=False),
        Scenario(
            "G1", "Geo: trips that passed within 300 m of a place during 90 days",
            "Incident investigation: which vehicles were near the address. Spherical distance on both sides.",
            locations,
            {
                "PostgreSQL": lambda loc: pg.tracks_near(loc[0], loc[1], 300, *window),
                "MongoDB": lambda loc: mongo.tracks_near(loc[0], loc[1], 300, *window),
            }),
        Scenario(
            "F1", "Flexible schema: reefer trips where cargo was warmer than 8 C",
            "Sensor exists only on some vehicles: EAV table in SQL, optional nested field in MongoDB.",
            list(range(heavy * 2)),
            {"PostgreSQL": lambda _: pg.cargo_violations(8.0), "MongoDB": lambda _: mongo.cargo_violations(8.0)},
            warmup=1, per_op=False),
        append_scenario(pg, mongo, seed, tracks=5 if quick else 10, live=40 if quick else 100),
    ]


def environment(pg: PostgresTelemetry, mongo: MongoTelemetry) -> dict:
    return {"date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "python": platform.python_version(),
            "os": platform.platform(), "postgres": pg.version(), "mongo": mongo.version()}


def fmt_bytes(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def write_report(env: dict, load: dict | None, results: list[tuple[Scenario, list[Measured]]]) -> str:
    lines = ["# Benchmark results", "",
             f"Date: {env['date']}. Python {env['python']}, PostgreSQL {env['postgres']}, MongoDB {env['mongo']}.",
             f"OS: {env['os']}.", ""]
    if load:
        pgs, ms = load["postgres"], load["mongo"]
        lines += ["## Data volume and bulk load (W1)", "",
                  f"Generated {load['tracks']} trips (seed {load['seed']}), loaded in batches of {load['batch_size']} trips.",
                  "",
                  "| Metric | PostgreSQL | PostgreSQL JSONB | MongoDB |",
                  "|---|---|---|---|",
                  f"| Rows / documents | tracks {pgs['tracks']}, points {pgs['points']}, readings {pgs['readings']}, "
                  f"events {pgs['events']} | {pgs['tracks']} documents | {ms['tracks']} documents |",
                  f"| Bulk load, s | {load['load_seconds']['PostgreSQL']} | "
                  f"{load['load_seconds']['PostgreSQL JSONB']} | {load['load_seconds']['MongoDB']} |",
                  f"| Trips per second | {load['tracks'] / load['load_seconds']['PostgreSQL']:.0f} | "
                  f"{load['tracks'] / load['load_seconds']['PostgreSQL JSONB']:.0f} | "
                  f"{load['tracks'] / load['load_seconds']['MongoDB']:.0f} |",
                  f"| Index build, s | {load['index_seconds']['PostgreSQL']} | - | "
                  f"{load['index_seconds']['MongoDB']} |",
                  f"| Size on disk (data + indexes) | {fmt_bytes(pgs['bytes'])} | {fmt_bytes(pgs['jsonb_bytes'])} | "
                  f"{fmt_bytes(ms['bytes'])} |", ""]
    lines += ["## Scenarios", "",
              "| Scenario | Engine | Median, ms | p95, ms | Runs | Ops/s | Result check |",
              "|---|---|---|---|---|---|---|"]
    for s, ms_ in results:
        for m in ms_:
            ops = f"{1000 / m.mean:.0f}" if s.per_op else "-"
            lines.append(f"| {s.key}. {s.title} | {m.engine} | {m.median:.2f} | {m.p95:.2f} | {len(m.times_ms)} | "
                         f"{ops} | {'same' if m.ok else 'DIFFERENT'} |")
    lines += ["", "## What each scenario checks", ""]
    lines += [f"- **{s.key}** — {s.why}" for s, _ in results]
    return "\n".join(lines) + "\n"


def run_all(pg: PostgresTelemetry, mongo: MongoTelemetry, quick: bool = False, seed: int = 7) -> str:
    load = None
    load_file = RESULTS_DIR / "load.json"
    if load_file.exists():
        load = json.loads(load_file.read_text(encoding="utf-8"))
    results = []
    live_ids: list[int] = []
    try:
        for s in scenarios(pg, mongo, quick, seed):
            if s.key == "W2":
                live_ids = sorted({op[0] for op in s.params})
            results.append((s, run(s)))
    finally:
        if live_ids:
            pg.delete_tracks(live_ids)
            mongo.delete_tracks(live_ids)

    env = environment(pg, mongo)
    report = write_report(env, load, results)
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "benchmark.md").write_text(report, encoding="utf-8")
    raw = {"environment": env, "load": load,
           "scenarios": [{"key": s.key, "title": s.title,
                          "engines": [{"engine": m.engine, "median_ms": round(m.median, 3), "p95_ms": round(m.p95, 3),
                                       "mean_ms": round(m.mean, 3), "runs": len(m.times_ms), "same_result": m.ok}
                                      for m in ms]} for s, ms in results]}
    (RESULTS_DIR / "benchmark.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
