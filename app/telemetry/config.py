import os
from pathlib import Path

PG_CONNINFO = os.environ.get(
    "FLEET_DB", "host=localhost port=5432 dbname=fleet_db user=postgres password=postgres")
MONGO_URI = os.environ.get("FLEET_MONGO", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("FLEET_MONGO_DB", "fleet_telemetry")
REDIS_URL = os.environ.get("FLEET_REDIS", "redis://localhost:6379/0")

LAB_DIR = Path(__file__).resolve().parents[2]
SQL_DIR = LAB_DIR / "sql"
RESULTS_DIR = LAB_DIR / "results"
