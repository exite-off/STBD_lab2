# Benchmark results

Date: 2026-11-12 15:40 UTC. Python 3.14.7, PostgreSQL 16.15, MongoDB 8.3.17.
OS: Windows-11-10.0.26300-SP0.

## Data volume and bulk load (W1)

Generated 2000 trips (seed 42), loaded in batches of 100 trips.

| Metric | PostgreSQL | PostgreSQL JSONB | MongoDB |
|---|---|---|---|
| Rows / documents | tracks 2000, points 958694, readings 2750199, events 86554 | 2000 documents | 2000 documents |
| Bulk load, s | 311.61 | 12.13 | 5.0 |
| Trips per second | 6 | 165 | 400 |
| Index build, s | 39.91 | - | 4.39 |
| Size on disk (data + indexes) | 530.6 MB | 41.3 MB | 70.4 MB |

## Scenarios

| Scenario | Engine | Median, ms | p95, ms | Runs | Ops/s | Result check |
|---|---|---|---|---|---|---|
| R1. Read one whole trip (points + sensors + events) by id | PostgreSQL (3 queries) | 9.34 | 15.28 | 300 | 102 | same |
| R1. Read one whole trip (points + sensors + events) by id | PostgreSQL (1 query, json_agg) | 18.28 | 24.68 | 300 | 55 | same |
| R1. Read one whole trip (points + sensors + events) by id | PostgreSQL JSONB | 3.76 | 5.16 | 300 | 264 | same |
| R1. Read one whole trip (points + sensors + events) by id | MongoDB | 3.07 | 4.44 | 300 | 311 | same |
| R2. List trips of a vehicle for the last 30 days (summary only) | PostgreSQL | 0.78 | 0.99 | 240 | 1270 | same |
| R2. List trips of a vehicle for the last 30 days (summary only) | MongoDB | 1.04 | 1.38 | 240 | 923 | same |
| A1. Monthly report per vehicle: trips, km, max speed, events | PostgreSQL | 8.76 | 9.38 | 5 | - | same |
| A1. Monthly report per vehicle: trips, km, max speed, events | MongoDB | 25.83 | 26.36 | 5 | - | same |
| A2. Average speed by hour of day over all GPS points | PostgreSQL | 307.62 | 308.32 | 5 | - | same |
| A2. Average speed by hour of day over all GPS points | MongoDB | 603.00 | 639.29 | 5 | - | same |
| G1. Geo: trips that passed within 300 m of a place during 90 days | PostgreSQL | 2.19 | 3.03 | 60 | 432 | same |
| G1. Geo: trips that passed within 300 m of a place during 90 days | MongoDB | 2.51 | 4.65 | 60 | 336 | same |
| F1. Flexible schema: reefer trips where cargo was warmer than 8 C | PostgreSQL | 25.71 | 27.83 | 10 | - | same |
| F1. Flexible schema: reefer trips where cargo was warmer than 8 C | MongoDB | 11.56 | 12.10 | 10 | - | same |
| W2. Live tracker: append one point to an active trip (10 trips x 100 points) | PostgreSQL | 47.94 | 51.87 | 1000 | 21 | same |
| W2. Live tracker: append one point to an active trip (10 trips x 100 points) | PostgreSQL JSONB | 8.10 | 9.09 | 1000 | 123 | same |
| W2. Live tracker: append one point to an active trip (10 trips x 100 points) | MongoDB | 4.55 | 5.42 | 1000 | 217 | same |

## What each scenario checks

- **R1** — Show a trip on the map: the whole aggregate is needed at once.
- **R2** — Typical list screen, only the summary fields, no points.
- **A1** — Aggregation over trip summaries plus counting events of every trip.
- **A2** — Analytics over individual points: in MongoDB they are inside arrays and must be unwound.
- **G1** — Incident investigation: which vehicles were near the address. Spherical distance on both sides.
- **F1** — Sensor exists only on some vehicles: EAV table in SQL, optional nested field in MongoDB.
- **W2** — Main write load of telemetry: small frequent writes into a growing trip.
