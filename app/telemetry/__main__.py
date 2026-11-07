import argparse
import json

from telemetry import config
from telemetry.stores.mongo import MongoTelemetry
from telemetry.stores.postgres import PostgresTelemetry


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m telemetry", description="Lab 2: telemetry in MongoDB vs PostgreSQL")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="create schemas, generate data and load it into every store")
    load.add_argument("--tracks", type=int, default=2000, help="number of trips to generate (default 2000)")
    load.add_argument("--seed", type=int, default=42)
    bench = sub.add_parser("bench", help="run benchmark scenarios and write results/benchmark.md")
    bench.add_argument("--quick", action="store_true", help="fewer repetitions")
    sub.add_parser("redis-demo", help="basic Redis queries for the fleet")
    args = parser.parse_args()

    if args.command == "redis-demo":
        from telemetry.redis_demo import run_demo
        run_demo()
        return

    pg = PostgresTelemetry(config.PG_CONNINFO)
    mongo = MongoTelemetry(config.MONGO_URI, config.MONGO_DB)
    try:
        if args.command == "load":
            from telemetry.loader import load_all
            result = load_all(pg, mongo, args.tracks, args.seed)
            config.RESULTS_DIR.mkdir(exist_ok=True)
            (config.RESULTS_DIR / "load.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
        else:
            from telemetry.bench import run_all
            print(run_all(pg, mongo, quick=args.quick))
    finally:
        pg.close()
        mongo.close()


if __name__ == "__main__":
    main()
